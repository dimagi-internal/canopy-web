# L2 — `Feedback` as a first-class object Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give feedback on a narrative a durable home that is independent of the channel it arrived through, and that never turns itself into work.

**Architecture:** One framework-tier Django app, `apps/feedback`, holding a single model generic over its target (`target_kind` + `target_ref`, never an FK to a product model) so it imports no product app. Three endpoints: batch ingest (idempotent per `(channel, source_ref)`), a filtered list, and a resolve. **No signals, no `Item` creation, no push** — the pool sits until a turn reads it.

**Tech Stack:** Django 5, Django Ninja 1.x, Pydantic v2, pytest.

**Repo:** `canopy-web` (this repo).

**Spec:** `docs/superpowers/specs/2026-07-26-narrative-storyboard-and-ownership-design.md`, section "L2 — `Feedback`".

**Depends on:** nothing. L0/L1 are canopy-runtime work; this is independent and can land in parallel.

## Global Constraints

- Work in a fresh git worktree off `origin/main`. Never commit to `main`.
- Backend tests: `uv run pytest`. Frontend typecheck: `cd frontend && npm run build`.
- **Framework tier.** `apps/feedback` must not import any product app (`projects`, `walkthroughs`, `reviews`, `shareouts`, `runs`, and later `storyboards`). Add it to `FRAMEWORK` in `tests/test_architecture_boundary.py` and to the tier table in `ARCHITECTURE.md` — the boundary test fails on a new app left untiered.
- **The target is a string pair, not an FK.** `target_kind="narrative"` + `target_ref="<slug>"`. A narrative is not even a table (`apps/runs/aggregate.py` infers it at read time), and an FK to a product model would break the tier. This mirrors how `Item` carries its own text rather than resolving a subject.
- **Emit nothing.** No `post_save` receiver, no `Item`, no push, no timeline event. If a later reader wants a signal they can add one; adding it now would make feedback into work, which is the thing the design refuses.
- Errors are RFC 7807 `application/problem+json` like every other route.
- **Regenerate the OpenAPI types and commit them** when `schemas.py`/`api.py` land: `cd frontend && npm run gen:api` (backend on :8000) or `npm run gen:api:local`. `regen-openapi.yml` fails the PR if `generated.ts` is stale.

## Scope note — who can write, in this layer

The spec describes a share-token-gated web submit. **L2 ships PAT + session auth only**, because the token that would gate an anonymous submit is minted by the `Storyboard` in L3 — there is no issuer for it yet. Inventing one here would mean building a second token model and retiring it a layer later.

So in L2: agents POST with a PAT (this is the email/gdoc path, and it is the one that matters first, since ACE already reads `ace@dimagi-ai.com`), and logged-in humans POST with a session. L3 adds the anonymous token path against the same endpoint.

---

### Task 1: The model

**Files:**
- Create: `apps/feedback/__init__.py`, `apps/feedback/apps.py`, `apps/feedback/models.py`
- Create: `apps/feedback/migrations/__init__.py`
- Modify: `config/settings/base.py` (add to `INSTALLED_APPS`)
- Modify: `tests/test_architecture_boundary.py` (add `"feedback"` to `FRAMEWORK`)
- Modify: `ARCHITECTURE.md` (tier table row)
- Test: `tests/test_feedback_model.py`

**Interfaces:**
- Produces: `apps.feedback.models.Feedback` with the fields below, and the constants `Feedback.KIND_COMMENT` / `KIND_SUGGESTION`, `Feedback.STATE_NEW` / `STATE_TRIAGED` / `STATE_ANSWERED` / `STATE_DECLINED`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_feedback_model.py`:

```python
"""Feedback is generic over its target and idempotent per (channel, source_ref)."""
from __future__ import annotations

import pytest
from django.db import IntegrityError, transaction

from apps.feedback.models import Feedback

pytestmark = pytest.mark.django_db


def _mk(**over):
    kwargs = dict(
        target_kind="narrative",
        target_ref="verified-monitoring",
        target_version=17,
        anchor_id="the-goal",
        kind=Feedback.KIND_COMMENT,
        body="The word 'back-check' means something specific here.",
        author_name="Sophie",
        author_email="sophie@example.org",
        channel=Feedback.CHANNEL_EMAIL,
        source_ref="<msg-1@mail>",
    )
    kwargs.update(over)
    return Feedback.objects.create(**kwargs)


def test_defaults_to_state_new():
    assert _mk().state == Feedback.STATE_NEW


def test_same_channel_and_source_ref_cannot_be_ingested_twice():
    _mk()
    with pytest.raises(IntegrityError), transaction.atomic():
        _mk()


def test_the_same_source_ref_on_a_different_channel_is_a_different_row():
    _mk()
    assert _mk(channel=Feedback.CHANNEL_GDOC).pk


def test_a_blank_source_ref_does_not_collide():
    """Web submits have no natural id — they must not dedupe against each other."""
    _mk(channel=Feedback.CHANNEL_WEB, source_ref="")
    assert _mk(channel=Feedback.CHANNEL_WEB, source_ref="").pk


def test_anchor_is_optional_for_whole_narrative_feedback():
    assert _mk(anchor_id="").pk


def test_a_suggestion_carries_proposed_text():
    fb = _mk(kind=Feedback.KIND_SUGGESTION, suggested_text="…a re-visit by a QC enumerator.")
    assert fb.suggested_text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_feedback_model.py -q`
Expected: FAIL — `ModuleNotFoundError: apps.feedback`

- [ ] **Step 3: Write the app scaffold**

`apps/feedback/__init__.py` — empty.

`apps/feedback/apps.py`:

```python
from django.apps import AppConfig


class FeedbackConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.feedback"
    label = "feedback"
```

`apps/feedback/migrations/__init__.py` — empty.

- [ ] **Step 4: Write the model**

`apps/feedback/models.py`:

```python
"""Feedback on a thing, from a person, via a channel.

Framework tier. Generic over its target on PURPOSE: ``target_kind`` +
``target_ref`` are strings, never an FK. A DDD narrative is not even a table
(``apps.runs.aggregate`` infers it from a run_id slug at read time), and an FK to
a product model would break the one-way framework→product rule. Same discipline
``Item`` follows — carry your own text, resolve nothing.

Feedback is INPUT TO A DECISION, not work. This app deliberately emits no
signal: no Item, no push, no timeline event. A turn reads the pool when the
owner is ready, clusters it, and proposes what to do. Auto-promoting it would
make a queue nobody asked for out of a thing whose whole value is that it can
sit unread.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models


class Feedback(models.Model):
    KIND_COMMENT = "comment"
    KIND_SUGGESTION = "suggestion"
    KIND_CHOICES = [(KIND_COMMENT, "Comment"), (KIND_SUGGESTION, "Suggestion")]

    CHANNEL_WEB = "web"
    CHANNEL_EMAIL = "email"
    CHANNEL_GDOC = "gdoc"
    CHANNEL_MANUAL = "manual"
    CHANNEL_API = "api"
    CHANNEL_CHOICES = [
        (CHANNEL_WEB, "Web"), (CHANNEL_EMAIL, "Email"), (CHANNEL_GDOC, "Google Doc"),
        (CHANNEL_MANUAL, "Manual"), (CHANNEL_API, "API"),
    ]

    STATE_NEW = "new"
    STATE_TRIAGED = "triaged"
    STATE_ANSWERED = "answered"
    STATE_DECLINED = "declined"
    STATE_CHOICES = [
        (STATE_NEW, "New"), (STATE_TRIAGED, "Triaged"),
        (STATE_ANSWERED, "Answered"), (STATE_DECLINED, "Declined"),
    ]

    # --- what it is about -------------------------------------------------
    target_kind = models.CharField(max_length=32)
    """"narrative" today; "storyboard" in L3. A string so this app never
    imports the product app that owns the target."""
    target_ref = models.CharField(max_length=200)
    target_version = models.IntegerField(null=True, blank=True)
    """The version the feedback was left against. The page FOLLOWS the current
    release, so a comment must remember the text that provoked it — this is what
    lets the UI say "left against v3, now v5"."""
    anchor_id = models.CharField(max_length=200, blank=True, default="")
    """Stable scene id (L0), or an act id. Blank = the whole thing."""

    # --- what it says ------------------------------------------------------
    kind = models.CharField(max_length=16, choices=KIND_CHOICES, default=KIND_COMMENT)
    body = models.TextField(blank=True, default="")
    suggested_text = models.TextField(blank=True, default="")
    """Proposed replacement narration when kind=suggestion. It reaches the
    narrative only through a turn the owner fires — never automatically."""

    # --- who said it -------------------------------------------------------
    author_name = models.CharField(max_length=200, blank=True, default="")
    author_email = models.CharField(max_length=320, blank=True, default="")
    """Free text: external reviewers have no accounts and never will."""
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="submitted_feedback",
    )
    """The authenticated caller, when there was one — the agent's PAT user for
    an ingested email, or the human for a logged-in submit. Never the external
    author, who has no account."""

    # --- how it arrived ----------------------------------------------------
    channel = models.CharField(max_length=16, choices=CHANNEL_CHOICES, default=CHANNEL_WEB)
    source_ref = models.CharField(max_length=500, blank=True, default="")
    """Opaque provenance for dedupe on re-ingest: an email Message-ID, a
    doc-id + comment-id. Blank for channels with no natural id (a web submit),
    which is why the uniqueness constraint excludes blanks."""

    # --- what happened to it ----------------------------------------------
    state = models.CharField(max_length=16, choices=STATE_CHOICES, default=STATE_NEW)
    disposition_note = models.TextField(blank=True, default="")
    resolved_in_version = models.IntegerField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["target_kind", "target_ref", "state"]),
            models.Index(fields=["channel", "source_ref"]),
        ]
        constraints = [
            # Re-ingesting the same email or doc comment must be a no-op. Blank
            # source_ref is exempt: a web submit has no natural id and two
            # people saying "this scene is confusing" are two pieces of
            # feedback, not a duplicate.
            models.UniqueConstraint(
                fields=["channel", "source_ref"],
                condition=~models.Q(source_ref=""),
                name="uniq_feedback_channel_source_ref",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"feedback:{self.target_kind}:{self.target_ref}:{self.pk}"
```

- [ ] **Step 5: Register the app and its tier**

In `config/settings/base.py`, add `"apps.feedback"` to `INSTALLED_APPS` beside the other framework apps.

In `tests/test_architecture_boundary.py`, add `"feedback"` to the `FRAMEWORK` set.

In `ARCHITECTURE.md`, add a row to the tier table:

```markdown
| `feedback` | **framework** | Feedback on a thing, from a person, via a channel (`web`/`email`/`gdoc`/`manual`/`api`). Generic over its target (`target_kind` + `target_ref` strings, never an FK) so it imports no product app — the same discipline `Item` follows. Deliberately emits no signal: feedback is input to a decision, not work. |
```

- [ ] **Step 6: Make the migration**

Run: `uv run python manage.py makemigrations feedback`
Expected: `0001_initial.py` created.

- [ ] **Step 7: Run tests to verify they pass**

Run: `uv run pytest tests/test_feedback_model.py tests/test_architecture_boundary.py -q`
Expected: all pass

- [ ] **Step 8: Commit**

```bash
git add apps/feedback config/settings/base.py tests/ ARCHITECTURE.md
git commit -m "feat(feedback): framework-tier Feedback model, generic over its target"
```

---

### Task 2: Services — batch ingest, list, resolve

**Files:**
- Create: `apps/feedback/services.py`
- Test: `tests/test_feedback_services.py`

**Interfaces:**
- Consumes: `Feedback` from Task 1.
- Produces:
  - `ingest(items: list[dict], *, submitted_by=None) -> dict` → `{"created": int, "duplicate": int, "ids": list[int]}`
  - `list_feedback(*, target_kind=None, target_ref=None, state=None, channel=None) -> QuerySet`
  - `resolve(pk: int, *, state: str, note: str = "", resolved_in_version: int | None = None) -> Feedback`

Request-free so the MCP surface and the REST views can share it, exactly as `apps/harness/schedule_services.py` does.

- [ ] **Step 1: Write the failing test**

Create `tests/test_feedback_services.py`:

```python
from __future__ import annotations

import pytest

from apps.feedback import services
from apps.feedback.models import Feedback

pytestmark = pytest.mark.django_db


def _item(**over):
    d = dict(
        target_kind="narrative", target_ref="verified-monitoring", target_version=17,
        anchor_id="the-goal", kind="comment", body="Say 'back-check', not 'audit'.",
        author_name="Sophie", channel="email", source_ref="<m1@mail>",
    )
    d.update(over)
    return d


def test_ingest_creates_rows():
    out = services.ingest([_item(), _item(source_ref="<m2@mail>")])
    assert out["created"] == 2
    assert out["duplicate"] == 0


def test_re_ingesting_the_same_source_ref_is_a_no_op():
    services.ingest([_item()])
    out = services.ingest([_item(body="edited in the mail client")])
    assert out["created"] == 0
    assert out["duplicate"] == 1
    assert Feedback.objects.count() == 1


def test_a_partial_duplicate_batch_still_creates_the_new_rows():
    services.ingest([_item()])
    out = services.ingest([_item(), _item(source_ref="<m3@mail>")])
    assert (out["created"], out["duplicate"]) == (1, 1)


def test_two_web_submits_without_a_source_ref_are_both_kept():
    out = services.ingest([
        _item(channel="web", source_ref=""),
        _item(channel="web", source_ref=""),
    ])
    assert out["created"] == 2


def test_list_filters_by_target_and_state():
    services.ingest([_item(), _item(target_ref="other", source_ref="<m9@mail>")])
    assert services.list_feedback(target_ref="verified-monitoring").count() == 1
    assert services.list_feedback(state="new").count() == 2


def test_resolve_records_the_disposition():
    pk = services.ingest([_item()])["ids"][0]
    fb = services.resolve(pk, state="answered", note="folded into v18", resolved_in_version=18)
    assert (fb.state, fb.resolved_in_version) == ("answered", 18)
    assert "v18" in fb.disposition_note


def test_ingest_emits_no_side_effects(django_assert_num_queries):
    """The whole point: feedback is inert until a turn reads it.

    If this ever fails because a signal was added, delete the signal — do not
    update the test. Auto-promotion is the design decision this app exists to
    refuse.
    """
    from django.db.models.signals import post_save
    received = []
    post_save.connect(lambda **kw: received.append(kw), sender=Feedback, weak=False)
    try:
        services.ingest([_item()])
    finally:
        post_save.disconnect(sender=Feedback)
    # Django's own post_save fires; what must NOT exist is an app receiver that
    # turns it into work. Assert no Item was created.
    from apps.harness.models import Item
    assert Item.objects.count() == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_feedback_services.py -q`
Expected: FAIL — `ModuleNotFoundError: apps.feedback.services`

- [ ] **Step 3: Write the services**

`apps/feedback/services.py`:

```python
"""Request-free service layer, so REST and (later) MCP share one implementation.

Mirrors apps/harness/schedule_services.py — the pattern that keeps the two
surfaces from drifting.
"""
from __future__ import annotations

from django.db import transaction

from apps.feedback.models import Feedback

_INGEST_FIELDS = (
    "target_kind", "target_ref", "target_version", "anchor_id",
    "kind", "body", "suggested_text",
    "author_name", "author_email", "channel", "source_ref",
)


def ingest(items: list[dict], *, submitted_by=None) -> dict:
    """Create feedback rows, skipping ones already ingested.

    Idempotent per ``(channel, source_ref)`` so re-reading a mailbox or a doc is
    safe. A blank ``source_ref`` never dedupes — a web submit has no natural id.
    The whole batch commits in one transaction.
    """
    created_ids: list[int] = []
    duplicate = 0

    with transaction.atomic():
        for raw in items:
            data = {k: raw.get(k) for k in _INGEST_FIELDS if raw.get(k) is not None}
            channel = data.get("channel") or Feedback.CHANNEL_WEB
            source_ref = data.get("source_ref") or ""

            if source_ref and Feedback.objects.filter(
                channel=channel, source_ref=source_ref
            ).exists():
                duplicate += 1
                continue

            fb = Feedback.objects.create(**data, submitted_by=submitted_by)
            created_ids.append(fb.pk)

    return {"created": len(created_ids), "duplicate": duplicate, "ids": created_ids}


def list_feedback(*, target_kind=None, target_ref=None, state=None, channel=None):
    qs = Feedback.objects.all()
    if target_kind:
        qs = qs.filter(target_kind=target_kind)
    if target_ref:
        qs = qs.filter(target_ref=target_ref)
    if state:
        qs = qs.filter(state=state)
    if channel:
        qs = qs.filter(channel=channel)
    return qs


def resolve(pk: int, *, state: str, note: str = "", resolved_in_version=None) -> Feedback:
    """Record what a decision turn did with one piece of feedback."""
    fb = Feedback.objects.get(pk=pk)
    fb.state = state
    if note:
        fb.disposition_note = note
    if resolved_in_version is not None:
        fb.resolved_in_version = resolved_in_version
    fb.save(update_fields=["state", "disposition_note", "resolved_in_version", "updated_at"])
    return fb
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_feedback_services.py -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add apps/feedback/services.py tests/test_feedback_services.py
git commit -m "feat(feedback): request-free service layer (ingest/list/resolve)"
```

---

### Task 3: The API

**Files:**
- Create: `apps/feedback/schemas.py`, `apps/feedback/api.py`
- Modify: `apps/api/api.py` (register the router)
- Test: `tests/test_feedback_api.py`

**Interfaces:**
- Consumes: `services` from Task 2.
- Produces: `POST /api/feedback/`, `GET /api/feedback/`, `POST /api/feedback/{id}/resolve`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_feedback_api.py`:

```python
from __future__ import annotations

import pytest

from apps.feedback.models import Feedback

pytestmark = pytest.mark.django_db


def _batch(**over):
    item = dict(
        target_kind="narrative", target_ref="verified-monitoring", target_version=17,
        anchor_id="the-goal", kind="comment",
        body="'Back-check' is the term of art; 'audit' means something else.",
        author_name="Sophie", channel="email", source_ref="<m1@mail>",
    )
    item.update(over)
    return {"items": [item]}


def test_post_requires_auth(client):
    r = client.post("/api/feedback/", _batch(), content_type="application/json")
    assert r.status_code in (401, 403)


def test_post_creates_and_is_idempotent(auth_client):
    first = auth_client.post("/api/feedback/", _batch(), content_type="application/json")
    assert first.status_code == 200, first.content
    assert first.json()["created"] == 1

    again = auth_client.post("/api/feedback/", _batch(), content_type="application/json")
    assert again.json() == {"created": 0, "duplicate": 1, "ids": []}
    assert Feedback.objects.count() == 1


def test_list_filters(auth_client):
    auth_client.post("/api/feedback/", _batch(), content_type="application/json")
    auth_client.post(
        "/api/feedback/", _batch(target_ref="other", source_ref="<m2@mail>"),
        content_type="application/json",
    )
    r = auth_client.get("/api/feedback/?target_ref=verified-monitoring")
    assert r.status_code == 200
    assert len(r.json()["items"]) == 1


def test_resolve_records_disposition(auth_client):
    post = auth_client.post("/api/feedback/", _batch(), content_type="application/json")
    fid = post.json()["ids"][0]
    r = auth_client.post(
        f"/api/feedback/{fid}/resolve",
        {"state": "answered", "note": "folded into v18", "resolved_in_version": 18},
        content_type="application/json",
    )
    assert r.status_code == 200
    assert r.json()["state"] == "answered"


def test_submitted_by_is_the_caller_not_the_author(auth_client, django_user_model):
    auth_client.post("/api/feedback/", _batch(), content_type="application/json")
    fb = Feedback.objects.get()
    assert fb.author_name == "Sophie"
    assert fb.submitted_by is not None
    assert fb.submitted_by.username != "Sophie"
```

Use whatever authenticated-client fixture the existing API tests use (check
`tests/conftest.py` — match it rather than inventing one).

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_feedback_api.py -q`
Expected: FAIL — 404, the route does not exist

- [ ] **Step 3: Write the schemas**

`apps/feedback/schemas.py`:

```python
from __future__ import annotations

from typing import Literal

from apps.common.schemas import StrictModel


class FeedbackIn(StrictModel):
    target_kind: str = "narrative"
    target_ref: str
    target_version: int | None = None
    anchor_id: str = ""
    kind: Literal["comment", "suggestion"] = "comment"
    body: str = ""
    suggested_text: str = ""
    author_name: str = ""
    author_email: str = ""
    channel: Literal["web", "email", "gdoc", "manual", "api"] = "web"
    source_ref: str = ""


class FeedbackBatchIn(StrictModel):
    items: list[FeedbackIn]


class FeedbackOut(StrictModel):
    id: int
    target_kind: str
    target_ref: str
    target_version: int | None
    anchor_id: str
    kind: str
    body: str
    suggested_text: str
    author_name: str
    author_email: str
    channel: str
    source_ref: str
    state: str
    disposition_note: str
    resolved_in_version: int | None
    created_at: str


class FeedbackListOut(StrictModel):
    items: list[FeedbackOut]


class FeedbackIngestOut(StrictModel):
    created: int
    duplicate: int
    ids: list[int]


class FeedbackResolveIn(StrictModel):
    state: Literal["new", "triaged", "answered", "declined"]
    note: str = ""
    resolved_in_version: int | None = None
```

If `StrictModel` lives at a different path, import it from wherever the other
apps' `schemas.py` take it.

- [ ] **Step 4: Write the router**

`apps/feedback/api.py`, following the shape of `apps/push/api.py`:

```python
"""Feedback ingest + read. Deliberately thin over services.

canopy-web is not an integration hub: it owns what happens IN canopy-web. Email
and Google-Doc feedback arrive because an AGENT reads them and POSTs here with
its PAT — there is no poller, no third-party credential, and no inbound
connector in this app. That is what keeps it generic over `channel`.
"""
from __future__ import annotations

from django.http import HttpRequest
from ninja import Router

from apps.feedback import services
from apps.feedback.schemas import (
    FeedbackBatchIn, FeedbackIngestOut, FeedbackListOut, FeedbackOut, FeedbackResolveIn,
)

router = Router(tags=["feedback"])


def _out(fb) -> dict:
    return {
        "id": fb.pk, "target_kind": fb.target_kind, "target_ref": fb.target_ref,
        "target_version": fb.target_version, "anchor_id": fb.anchor_id,
        "kind": fb.kind, "body": fb.body, "suggested_text": fb.suggested_text,
        "author_name": fb.author_name, "author_email": fb.author_email,
        "channel": fb.channel, "source_ref": fb.source_ref, "state": fb.state,
        "disposition_note": fb.disposition_note,
        "resolved_in_version": fb.resolved_in_version,
        "created_at": fb.created_at.isoformat(),
    }


@router.post("/", response=FeedbackIngestOut, summary="Ingest feedback (batch, idempotent)")
def ingest_feedback(request: HttpRequest, payload: FeedbackBatchIn) -> dict:
    return services.ingest(
        [i.model_dump() for i in payload.items],
        submitted_by=request.user if request.user.is_authenticated else None,
    )


@router.get("/", response=FeedbackListOut, summary="List feedback")
def list_feedback(
    request: HttpRequest,
    target_kind: str | None = None,
    target_ref: str | None = None,
    state: str | None = None,
    channel: str | None = None,
) -> dict:
    qs = services.list_feedback(
        target_kind=target_kind, target_ref=target_ref, state=state, channel=channel
    )
    return {"items": [_out(fb) for fb in qs]}


@router.post("/{feedback_id}/resolve", response=FeedbackOut, summary="Record a disposition")
def resolve_feedback(request: HttpRequest, feedback_id: int, payload: FeedbackResolveIn) -> dict:
    fb = services.resolve(
        feedback_id, state=payload.state, note=payload.note,
        resolved_in_version=payload.resolved_in_version,
    )
    return _out(fb)
```

Return a 404 problem response when `resolve` hits a missing row — match how the
neighbouring apps raise it (check `apps/push/api.py` / `apps/harness/api.py`).

- [ ] **Step 5: Register the router**

In `apps/api/api.py`, beside the other framework routers:

```python
from apps.feedback.api import router as feedback_router  # noqa: E402
```

and add it to the mount list with prefix `feedback`.

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_feedback_api.py -q`
Expected: all pass

- [ ] **Step 7: Regenerate the OpenAPI types**

```bash
uv run python manage.py runserver &   # or use npm run gen:api:local
cd frontend && npm run gen:api
```

Confirm `frontend/src/api/generated.ts` gained the feedback paths, and commit it —
`regen-openapi.yml` fails the PR if it is stale.

- [ ] **Step 8: Run the whole suite**

Run: `uv run pytest -q && cd frontend && npm run build`
Expected: both pass

- [ ] **Step 9: Commit**

```bash
git add apps/feedback apps/api/api.py frontend/src/api/generated.ts tests/
git commit -m "feat(feedback): ingest/list/resolve API + generated types"
```

---

### Task 4: The no-signal guard

**Files:**
- Test: `tests/test_feedback_emits_nothing.py`

**Interfaces:** none — this is a design guard.

The single most important property of this app is what it does NOT do. A future
contributor will reasonably think "feedback should notify someone" and wire a
receiver. This test is the argument against that, in executable form.

- [ ] **Step 1: Write the test**

```python
"""apps.feedback emits no signal. This is a design decision, not an oversight.

Feedback is INPUT TO A DECISION. The owner fires a turn when ready; the turn
reads the pool, clusters it, and proposes dispositions. Auto-promoting each
piece into an Item would rebuild the queue-grooming step the inbox redesign
deliberately removed, and would mean an external reviewer could enqueue work.

If you are here because you added a notification and this failed: the
notification is the thing to remove.
"""
from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP = ROOT / "apps" / "feedback"


def test_the_app_has_no_signals_module():
    assert not (APP / "signals.py").exists(), (
        "apps/feedback must not emit signals — feedback is input to a decision, "
        "not work. See the module docstring in apps/feedback/models.py."
    )


def test_no_module_connects_a_receiver_or_creates_an_item():
    banned = ("post_save", "post_delete", "pre_save", "on_commit", "Item(", "Item.objects.create")
    offenders = []
    for path in APP.rglob("*.py"):
        text = path.read_text()
        for token in banned:
            if token in text:
                offenders.append(f"{path.relative_to(ROOT)}: {token}")
    assert offenders == [], f"apps/feedback grew a side effect: {offenders}"
```

- [ ] **Step 2: Run it**

Run: `uv run pytest tests/test_feedback_emits_nothing.py -q`
Expected: 2 passed

- [ ] **Step 3: Commit**

```bash
git add tests/test_feedback_emits_nothing.py
git commit -m "test(feedback): guard that the app emits no signal, by design"
```

---

## Self-review

**Spec coverage.** The model and its every field (Task 1), the three endpoints
with idempotent batch ingest (Tasks 2–3), framework tiering (Task 1 Step 5), and
"emits no signal" (Task 4). The share-token web submit is explicitly deferred to
L3 with a stated reason — there is no token issuer until `Storyboard` exists.

**Type consistency.** `ingest(items, *, submitted_by) -> {"created","duplicate","ids"}`
is identical in the service, its test, and the API test. `resolve(pk, *, state,
note, resolved_in_version)` likewise. Field names on `FeedbackIn` match the
model's exactly, which is what lets `services.ingest` splat them.

**Deliberate omissions.** No pagination on the list (the pool is per-narrative
and small; add it when a real list is slow). No `PATCH` — the only mutation is
`resolve`. No admin registration. No MCP tools yet: the service layer is
request-free so they are cheap to add, but nothing needs them.

**The one risk.** The uniqueness constraint uses a partial index
(`condition=~Q(source_ref="")`). That is PostgreSQL-native and the project is
Postgres-only, so it is fine here — but it means a blank `source_ref` can never
dedupe, which is deliberate and tested
(`test_two_web_submits_without_a_source_ref_are_both_kept`).
