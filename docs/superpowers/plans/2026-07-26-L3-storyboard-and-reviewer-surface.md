# L3 — Storyboard + reviewer surface Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a set of DDD narratives shareable as one ordered arc that an outsider can read and comment on, without ever showing them the machinery.

**Architecture:** A product-tier `apps/storyboards` holding `Storyboard → Act → Entry`, where an entry points at a narrative by slug and the page *follows* that narrative's current release. One capability-bearing share token per storyboard, gating anonymous read / comment / suggest — the anonymous write path L2 deferred. Two frontend surfaces: a clean per-narrative reviewer view (which becomes the default, with the operator console behind a link) and the storyboard page itself.

**Tech Stack:** Django 5, Django Ninja, Pydantic v2, React 19, Tailwind 4, `canopy-ui`.

**Repo:** `canopy-web`.

**Spec:** `docs/superpowers/specs/2026-07-26-narrative-storyboard-and-ownership-design.md`, section "L3".

**Depends on:** L2 (`apps/feedback`) — the storyboard's comment affordance writes `Feedback` rows with `target_kind="storyboard"` or `"narrative"`. This branch is stacked on `feat/feedback-object`.

## Global Constraints

- Work in a worktree off `feat/feedback-object` (or `main` once L2 lands). Never commit to `main`.
- Backend: `uv run pytest`. Frontend: `cd frontend && npm run build`.
- **Product tier.** `apps/storyboards` curates DDD narratives, which are product — add it to `PRODUCT` in `tests/test_architecture_boundary.py` and to the `ARCHITECTURE.md` tier table. It may import framework (`feedback`, `workspaces`) freely.
- **Follow, don't freeze.** An entry resolves to the narrative's current release at read time. `pinned_run_id` exists but stays null except when deliberately holding an entry on a known-good run while that narrative is mid-redraft.
- **Every `Feedback` row records `target_version`.** That is what lets the UI say "left against v3, now v5" on a page that follows.
- Anonymous access follows `apps/runs/api.py::get_run_release` exactly: `auth=None` on the route, the handler self-enforces (member OR matching `?t=` token), and the login middleware allowlists the path. A missing or wrong token 404s — never 403 — so existence does not leak, the same rule walkthroughs follow.
- Regenerate `frontend/src/api/generated.ts` and commit it.

---

### Task 1: The `Storyboard` model

**Files:**
- Create: `apps/storyboards/{__init__,apps,models}.py`, `apps/storyboards/migrations/__init__.py`
- Modify: `config/settings/base.py`, `tests/test_architecture_boundary.py`, `ARCHITECTURE.md`
- Test: `tests/test_storyboard_model.py`

**Interfaces:**
- Produces: `Storyboard`, `Act`, `Entry`; `Storyboard.CAP_READ/CAP_COMMENT/CAP_SUGGEST`; `Storyboard.ensure_share_token()` and `rotate_share_token()`.

- [ ] **Step 1: Write the failing test**

```python
"""A storyboard is ordered acts over narratives, with one capability-bearing token."""
from __future__ import annotations

import pytest

from apps.storyboards.models import Act, Entry, Storyboard
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db


@pytest.fixture()
def ws():
    return Workspace.objects.create(slug="dimagi", name="Dimagi")


def _board(ws, **over):
    kwargs = dict(slug="ecf-supply", title="What the money bought", workspace=ws)
    kwargs.update(over)
    return Storyboard.objects.create(**kwargs)


def test_acts_and_entries_keep_their_order(ws):
    b = _board(ws)
    a2 = Act.objects.create(storyboard=b, title="The command centre", position=2)
    a1 = Act.objects.create(storyboard=b, title="Six weeks to a supply base", position=1)
    Entry.objects.create(act=a1, narrative_slug="procurement", position=1)
    Entry.objects.create(act=a1, narrative_slug="registry", position=0)

    assert [a.title for a in b.acts.all()] == [a1.title, a2.title]
    assert [e.narrative_slug for e in a1.entries.all()] == ["registry", "procurement"]


def test_a_slug_is_unique_per_workspace_not_globally(ws):
    other = Workspace.objects.create(slug="connect", name="Connect")
    _board(ws)
    assert _board(other).pk  # same slug, different tenant — fine


def test_ensure_share_token_is_idempotent(ws):
    b = _board(ws)
    first = b.ensure_share_token()
    assert first and b.ensure_share_token() == first


def test_rotate_share_token_kills_the_old_link(ws):
    b = _board(ws)
    old = b.ensure_share_token()
    assert b.rotate_share_token() != old


def test_capability_defaults_to_read_only(ws):
    assert _board(ws).capability == Storyboard.CAP_READ


def test_pinning_an_entry_is_possible_but_not_the_default(ws):
    b = _board(ws)
    act = Act.objects.create(storyboard=b, title="Act", position=1)
    e = Entry.objects.create(act=act, narrative_slug="verified-monitoring", position=0)
    assert e.pinned_run_id == ""
```

- [ ] **Step 2: Run it — expect `ModuleNotFoundError`**

- [ ] **Step 3: Write the models**

`apps/storyboards/models.py`. Key decisions to encode in docstrings:

- `Storyboard.slug` unique **per workspace**, not globally (`UniqueConstraint(fields=["workspace", "slug"])`) — two tenants may both have a `supply` board.
- `capability` is a single choice on the board (`read` / `comment` / `suggest`), not per-token. One token, one grant. If you later need "Ellyn comments, Sophie suggests", that is a second token model — do not pre-build it.
- `Act.position` / `Entry.position` are plain integers with `ordering = ["position"]`. No fancy ordered-model dependency.
- `Entry.narrative_slug` is a **string**, like `Feedback.target_ref` — narratives are inferred at read time, not a table.
- `Entry.pinned_run_id` blank by default; comment must say it is the exception (holding an entry on a known-good run while that narrative is mid-redraft), not the norm.
- Token helpers mirror `apps/walkthroughs/models.py` (`ensure_share_token` / `rotate_share_token`) — reuse the same secrets-based generation rather than inventing one.

- [ ] **Step 4: Register + tier + migrate**

`INSTALLED_APPS`, `PRODUCT` set in the boundary test, `ARCHITECTURE.md` row, then
`uv run python manage.py makemigrations storyboards`.

- [ ] **Step 5: Run tests; Step 6: Commit**

---

### Task 2: Resolving a storyboard (the read model)

**Files:**
- Create: `apps/storyboards/services.py`
- Test: `tests/test_storyboard_resolve.py`

**Interfaces:**
- Produces: `resolve_board(board, *, request) -> dict` — acts → entries → each entry's current release (hero video, title, lede, scene count), by calling into `apps.runs.aggregate`.

This is the task where "follow, don't freeze" becomes real.

- [ ] **Step 1: Write the failing test**

Cover: an entry resolves to the narrative's newest run; a `pinned_run_id` overrides that; an entry whose narrative has **no** release yet resolves to a placeholder rather than raising (a storyboard authored before a narrative is rendered must still render).

- [ ] **Step 2–4: implement, test, commit**

`apps/storyboards` is product tier, so importing `apps.runs.aggregate` is legal and expected. Do NOT duplicate the release-building logic — call it.

---

### Task 3: The API

**Files:**
- Create: `apps/storyboards/{schemas,api}.py`
- Modify: `apps/api/api.py`, `apps/common/middleware.py` (allowlist the public path)
- Test: `tests/test_storyboard_api.py`

**Endpoints:**

```
GET    /api/storyboards/                    list (member)
POST   /api/storyboards/                    create (member)
GET    /api/storyboards/{slug}              detail — auth=None, self-enforcing
PATCH  /api/storyboards/{slug}              retitle / reorder / set capability (member)
POST   /api/storyboards/{slug}/rotate-token (member)
```

- [ ] **Step 1: Write the failing test**

The security cases are the ones that matter, and they must be explicit:

```python
def test_anonymous_without_a_token_404s_not_403(client, board):
    """404, never 403 — a wrong token must not confirm the board exists.
    Same rule walkthroughs follow."""
    assert client.get(f"/api/storyboards/{board.slug}").status_code == 404


def test_anonymous_with_a_wrong_token_404s(client, board):
    board.ensure_share_token()
    assert client.get(f"/api/storyboards/{board.slug}?t=nope").status_code == 404


def test_anonymous_with_the_right_token_reads(client, board):
    t = board.ensure_share_token()
    assert client.get(f"/api/storyboards/{board.slug}?t={t}").status_code == 200


def test_rotating_the_token_kills_the_old_link(client, board):
    old = board.ensure_share_token()
    board.rotate_share_token()
    assert client.get(f"/api/storyboards/{board.slug}?t={old}").status_code == 404


def test_a_non_member_cannot_read_another_tenants_board(other_ws_client, board):
    assert other_ws_client.get(f"/api/storyboards/{board.slug}").status_code == 404
```

- [ ] **Steps 2–6: implement, register, allowlist, test, commit**

Model the handler on `apps/runs/api.py::get_run_release` — `auth=None`, self-enforcing, `auto_join_workspaces` for authed members.

---

### Task 4: Anonymous feedback — the capability gate (closes L2's deferral)

**Files:**
- Modify: `apps/feedback/api.py`
- Test: `tests/test_feedback_anonymous.py`

**Interfaces:**
- Produces: `POST /api/feedback/` accepts an anonymous submit carrying `?t=<storyboard token>`, gated by the board's capability.

This is the one place L3 reaches back into a framework app. Keep the coupling one-way and thin: `apps/feedback` must NOT import `apps.storyboards` (that would invert the tier). Instead:

**Resolve the token in the storyboards app and pass the verdict in.** Concretely — add an optional `token_grant` seam to the feedback route that a product-side resolver populates, or expose the anonymous submit as a route on `apps/storyboards` that calls `feedback.services.ingest`. **Prefer the second**: the storyboard owns the token, so it owns the route that accepts a token-bearing write, and `apps/feedback` stays a store with no knowledge of who is allowed to write.

- [ ] **Step 1: Write the failing test**

```python
def test_a_read_only_link_cannot_comment(client, board):
    t = board.ensure_share_token()          # capability defaults to read
    r = client.post(f"/api/storyboards/{board.slug}/feedback?t={t}", COMMENT, ...)
    assert r.status_code == 403


def test_a_comment_link_can_comment_but_not_suggest(client, board):
    board.capability = Storyboard.CAP_COMMENT; board.save()
    t = board.ensure_share_token()
    assert post_comment(t).status_code == 200
    assert post_suggestion(t).status_code == 403


def test_an_anonymous_comment_records_no_submitted_by(client, board):
    """The external author has no account. author_name is free text; the
    caller field stays null rather than borrowing someone else's identity."""
    ...
    assert Feedback.objects.get().submitted_by is None


def test_the_version_the_comment_was_left_against_is_recorded(client, board):
    """The page FOLLOWS the current release, so without this the comment loses
    its anchor the moment the narrative moves."""
    ...
    assert Feedback.objects.get().target_version == 17
```

- [ ] **Steps 2–4: implement, test, commit**

---

### Task 5: The reviewer surface (frontend)

**Files:**
- Create: `frontend/src/pages/NarrativeReviewPage.tsx`
- Modify: `frontend/src/router.tsx`, `frontend/src/pages/ReviewPage.tsx`
- Test: `frontend/src/pages/NarrativeReviewPage.test.tsx`

One narrative, scene by scene, before/after when a prior version exists. **Reuses `pairNarrationScenes`** (`frontend/src/components/ddd/narrativeScenePairing.ts`) — do not write a second pairing function; that file already handles reorder-safety and the L0 work made its `id` assumption true.

Shows: the story, per-scene, with a comment box always and inline-editable text when the link grants `suggest`. Shows nothing about gates, features, provenance, actionability, or findings.

- [ ] **Step 1–6: test, build, wire the route, commit**

**The demotion:** `ReviewPage.tsx` (1,874 lines) keeps gates and findings but stops being the front door for narrative agreement — link to it as "show the build view". The #290 complaint was *"something only I understand"*; a second-class copy for outsiders would not have fixed that.

---

### Task 6: The storyboard page (frontend)

**Files:**
- Create: `frontend/src/pages/StoryboardPage.tsx`
- Modify: `frontend/src/router.tsx`
- Test: `frontend/src/pages/StoryboardPage.test.tsx`

Route `/storyboard/:slug` — **outside** the app shell (`PublicLayout`), exactly like `DddReleasePage`, so a token-bearing viewer with no Dimagi login is served.

Reads: lede → act prose → per-narrative card (hero video, one line, "read the scenes" → the reviewer surface) → feedback affordance at act and narrative level. Model the visual language on `DddReleasePage.tsx`, which is already the outsider-legible face of a single run — this is the same voice one level up.

- [ ] **Step 1–6: test, build, wire, commit**

---

### Task 7: Authoring from a repo

**Files:**
- Create: `apps/storyboards/management/commands/import_storyboard.py`
- Test: `tests/test_storyboard_import.py`

Agents author a `storyboard.yaml` in the product repo and push it. Idempotent per `(workspace, slug)`: re-importing updates titles/prose/order rather than duplicating.

```yaml
slug: ecf-supply
title: What the money bought
lede: Three acts, from the first purchase order to the child who recovered.
acts:
  - title: Six weeks to a supply base
    prose: Procurement integrity you can show, not assert.
    entries: [procurement-eoi, supplier-registry]
  - title: Where the RUTF is, and who is short
    entries: [command-centre]
```

- [ ] **Step 1–4: test, implement, commit**

---

## Self-review

**Spec coverage.** Storyboard model + acts + entries (T1), follow-don't-freeze (T2), the public token-gated read (T3), the anonymous capability-gated write that L2 deferred (T4), the clean reviewer surface + operator-console demotion (T5), the storyboard page (T6), agent authoring (T7).

**The tier trap, and how T4 avoids it.** The obvious implementation of anonymous feedback has `apps/feedback` resolving a storyboard token — which inverts the boundary, since `feedback` is framework and `storyboards` is product. The plan puts the token-bearing route on `apps/storyboards` instead, calling `feedback.services.ingest`. That keeps `feedback` a store that knows nothing about who may write, and it is why L2 built a request-free service layer.

**Deliberately not built.** Per-token capabilities (one grant per board is enough until it isn't). Comment threading. Notifications. Act-level videos. A storyboard-of-storyboards.

**Risk to watch.** Task 2 must not duplicate release-building logic. If `apps.runs.aggregate` does not expose a callable that returns one narrative's current release, add one there rather than reimplementing it in `storyboards` — a second copy of that logic is exactly the drift this whole effort is about.
