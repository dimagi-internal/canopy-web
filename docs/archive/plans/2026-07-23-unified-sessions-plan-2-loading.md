# Unified Runner Sessions — Plan 2: Tail-First Loading Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop shipping full session history to clients by default. The REST `GET /api/chat/{id}` transcript load and the WebSocket `session.state` connect snapshot both become **tail-first** — the last N messages (chronological) — with a backward cursor (`has_more_before` + `oldest_loaded_turn_index`), a cursor-based scroll-back endpoint for "Load earlier," and an explicit `?full=true` escape hatch so nothing is permanently hidden. Server-side storage is unchanged; only the server→client *shipping* is capped.

**Architecture:** One named constant, `SESSION_TAIL_DEFAULT`, is the tail size shared by the REST handler, the WS consumer, and the tests. Three small read helpers in `apps/canopy_sessions/services.py` (`tail_messages`, `messages_before`, `all_messages`) do the windowed queries; the REST handler and the WS `_snapshot` both call `tail_messages` so they can't drift. `GET /api/chat/{id}` returns the tail + cursor fields on `SessionDetailOut` (with `?full=true` to bypass the cap); a new `GET /api/chat/{id}/messages?before=…` pages backward for scroll-back. The WS `session.state` frame keeps its exact shape — only its `messages` array shrinks from the head `[:200]` to the tail. No model change, no migration: the cursor fields are computed, not stored.

**Tech Stack:** Django 5 ASGI, Django-Ninja + Pydantic v2, Django Channels, PostgreSQL, pytest.

## Global Constraints

- **No backwards compatibility, no data preservation.** Single user; no other consumers. Nothing here needs a compat shim.
- **Tail-first is the invariant.** After this plan, no default server→client path ships a full transcript. The tail size is a single named constant (`SESSION_TAIL_DEFAULT`), imported by both the REST handler and the WS consumer and referenced by the tests — never a magic `20` in two places, never a per-surface literal that can drift.
- **The `session.state` frame shape is frozen.** `serializers.session_state_dto` keeps its exact keys (`messages, active_draft, participants, presence_user_ids, current_user_id`); only the length of `messages` changes. The `canopy-ui/chat` reducer treats `session.state` as a full state replacement (`sessionReducer.ts` `case "session.state": return frame.data`), so a shorter `messages` array Just Works — scroll-back UI that *prepends* older messages is Plan 4.
- **Framework boundary holds.** `apps/canopy_sessions` is a framework app; no framework→product imports. `tests/test_architecture_boundary.py` must stay green (this plan adds no imports that could cross the boundary).
- **Every schema/api change regenerates types:** `cd frontend && npm run gen:api` (backend up on :8000) or `npm run gen:api:local` (against a dumped `openapi.json`), then commit `frontend/src/api/generated.ts`. **The `regen-openapi` CI job fails the PR if `generated.ts` is stale** — Plan 1 hit this gate; do NOT skip the regen step on Tasks 2 and 3. It does not auto-commit for you.
- Run backend tests with `uv run pytest`. Run one test: `uv run pytest tests/path::name -v`.

## Deferred to later plans (do NOT build here)

- **Scroll-back / "Load full" UI wiring** (the "Load earlier" button, prepending older pages into `SessionState`, the runner-offline state) — **Plan 4**. Plan 2 delivers the API + tests only; `ChatPage`/`useSessionSocket`/`ChatPanel` keep rendering the tail unchanged.
- **Per-runner tiered persistence + on-demand backfill from the runner** (local sessions with no `Message` rows pulling history off the laptop transcript) — **Plan 3**. Plan 2 is the client LOADING contract for *server-stored* `Message` rows; a local session that has no rows yet simply returns an empty/short tail — Plan 3 makes scroll-back promote it by backfilling from the runner.

## Context (verified against the tree at plan time)

- `apps/canopy_sessions/api.py::get_session` (`:85-92`) returns **all** messages, uncapped: `MessageOut.from_orm(m) for m in session.messages.order_by("turn_index")`.
- `apps/canopy_sessions/consumers.py::_snapshot` (`:241-256`) ships the **head** of the transcript: `messages = list(self.session.messages.order_by("turn_index")[:200])`.
- `apps/canopy_sessions/schemas.py::SessionDetailOut` (`:43-44`) is `SessionOut` + `messages: list[MessageOut]`.
- The router mounts at `/api/chat` (`apps/api/api.py:189`); the WS path is `ws/chat/{id}/`. Both prefixes are unchanged this plan.
- **`ChatPage.tsx` consumes `getSession` only for `meta.title`** — the transcript arrives over the WebSocket, not REST (`frontend/src/pages/ChatPage.tsx:34`). So adding fields to `SessionDetailOut` cannot break the page's render; it only changes the `ChatSessionDetail` generated type.
- No existing test asserts a snapshot message count or the `[:200]` head (`tests/test_chat_session_consumer.py` checks the frame's *keys*, not `len(messages)`); `tests/test_chat_api.py` asserts `detail["messages"]` for 0- and 2-message sessions — both well under the tail default, so they stay green.

---

### Task 1: Shared tail constant + windowed read helpers

The one home for the tail size and the three windowed queries every surface needs. Pure read helpers on `Session.messages` — no model change, no migration. Putting them in `services.py` means both `api.py` (imports `services`) and `consumers.py` (imports `services as chat_services`) reference the SAME constant and helpers, so REST and WS can't drift.

**Files:**
- Modify: `apps/canopy_sessions/services.py` (add constants + helpers near the top, below the imports)
- Test: `tests/test_session_loading.py` (new)

**Interfaces:**
- Produces: `services.SESSION_TAIL_DEFAULT` (int, `20`) and `services.SCROLLBACK_PAGE_DEFAULT` (int, `50`).
- Produces: `services.tail_messages(session, limit=None) -> tuple[list[Message], bool, int | None]` → `(messages_chronological, has_more_before, oldest_loaded_turn_index)`. `limit=None` ⇒ `SESSION_TAIL_DEFAULT`.
- Produces: `services.messages_before(session, before, limit=None) -> tuple[list[Message], bool]` → the window of up to `limit` messages with `turn_index < before`, chronological, plus `has_more_before`. `limit=None` ⇒ `SCROLLBACK_PAGE_DEFAULT`.
- Produces: `services.all_messages(session) -> tuple[list[Message], bool, int | None]` → every message chronological, `has_more_before=False`, oldest index (or `None`). The `?full=true` backend.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_session_loading.py
import pytest

from apps.canopy_sessions import services
from apps.canopy_sessions.models import Message, Session
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db


def _session_with(n: int) -> Session:
    ws = Workspace.objects.create(slug="w1", display_name="W1")
    s = Session.objects.create(workspace=ws, title="t")
    for i in range(n):
        Message.objects.create(
            session=s, turn_index=i, role=Message.USER, plaintext=f"m{i}",
        )
    return s


def test_tail_default_is_20():
    assert services.SESSION_TAIL_DEFAULT == 20


def test_tail_returns_last_n_chronological_with_cursor():
    s = _session_with(50)
    msgs, has_more, oldest = services.tail_messages(s)
    assert [m.turn_index for m in msgs] == list(range(30, 50))  # last 20, ascending
    assert has_more is True
    assert oldest == 30


def test_tail_short_session_has_no_more():
    s = _session_with(3)
    msgs, has_more, oldest = services.tail_messages(s)
    assert [m.turn_index for m in msgs] == [0, 1, 2]
    assert has_more is False
    assert oldest == 0


def test_tail_empty_session():
    s = _session_with(0)
    msgs, has_more, oldest = services.tail_messages(s)
    assert msgs == []
    assert has_more is False
    assert oldest is None


def test_messages_before_pages_backward():
    s = _session_with(50)
    # scroll-back from the tail's oldest (30), page size 10
    page, has_more = services.messages_before(s, before=30, limit=10)
    assert [m.turn_index for m in page] == list(range(20, 30))  # chronological window
    assert has_more is True
    # walk to the beginning
    page, has_more = services.messages_before(s, before=10, limit=10)
    assert [m.turn_index for m in page] == list(range(0, 10))
    assert has_more is False  # nothing older than index 0


def test_all_messages_returns_everything():
    s = _session_with(50)
    msgs, has_more, oldest = services.all_messages(s)
    assert len(msgs) == 50
    assert has_more is False
    assert oldest == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_session_loading.py -v`
Expected: FAIL — `AttributeError: module 'apps.canopy_sessions.services' has no attribute 'SESSION_TAIL_DEFAULT'`.

- [ ] **Step 3: Add the constants + helpers to `services.py`**

In `apps/canopy_sessions/services.py`, below the `_ROLE_FOR_KIND` block, add:

```python
# --- Tail-first loading contract (Plan 2) ---------------------------------
# The server never ships a full transcript by default. SESSION_TAIL_DEFAULT is
# the single home for the tail size, shared by the REST handler and the WS
# snapshot so the two can't drift; SCROLLBACK_PAGE_DEFAULT is the "Load earlier"
# page size (aligned with apps/realtime's cursor-paging conventions).
SESSION_TAIL_DEFAULT = 20
SCROLLBACK_PAGE_DEFAULT = 50


def tail_messages(session: Session, limit: int | None = None):
    """The last `limit` messages, chronological, plus a backward cursor.

    Returns (messages, has_more_before, oldest_loaded_turn_index). This is what
    a client gets by default — enough to continue, never the whole history.
    """
    limit = SESSION_TAIL_DEFAULT if limit is None else limit
    newest_first = list(session.messages.order_by("-turn_index")[:limit])
    messages = list(reversed(newest_first))
    if not messages:
        return [], False, None
    oldest = messages[0].turn_index
    has_more = session.messages.filter(turn_index__lt=oldest).exists()
    return messages, has_more, oldest


def messages_before(session: Session, before: int, limit: int | None = None):
    """The window of up to `limit` messages immediately older than `before`
    (exclusive), chronological, plus whether anything older still exists.

    Returns (messages, has_more_before). Drives the scroll-back endpoint.
    """
    limit = SCROLLBACK_PAGE_DEFAULT if limit is None else limit
    newest_first = list(
        session.messages.filter(turn_index__lt=before).order_by("-turn_index")[:limit]
    )
    messages = list(reversed(newest_first))
    if not messages:
        return [], False
    has_more = session.messages.filter(turn_index__lt=messages[0].turn_index).exists()
    return messages, has_more


def all_messages(session: Session):
    """Every message, chronological — the explicit "load full session" escape
    hatch. Returns (messages, has_more_before=False, oldest_turn_index)."""
    messages = list(session.messages.order_by("turn_index"))
    if not messages:
        return [], False, None
    return messages, False, messages[0].turn_index
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_session_loading.py -v`
Expected: PASS (all six).

- [ ] **Step 5: Commit**

```bash
git add apps/canopy_sessions/services.py tests/test_session_loading.py
git commit -m "feat(sessions): shared SESSION_TAIL_DEFAULT + windowed read helpers"
```

---

### Task 2: REST `GET /api/chat/{id}` returns the tail + cursor (with `?full=true`)

Cap the transcript load. The detail response gains `has_more_before` + `oldest_loaded_turn_index`; the handler returns `tail_messages` by default and `all_messages` when `?full=true`. This changes `SessionDetailOut`'s shape ⇒ regenerate `generated.ts`.

**Files:**
- Modify: `apps/canopy_sessions/schemas.py` (`SessionDetailOut`, `:43-44`)
- Modify: `apps/canopy_sessions/api.py` (`get_session`, `:85-92`)
- Modify (regen, do not hand-edit): `frontend/src/api/generated.ts`
- Test: `tests/test_session_loading.py` (append REST cases)

**Interfaces:**
- Consumes: `services.tail_messages`, `services.all_messages`, `services.SESSION_TAIL_DEFAULT`.
- Produces: `SessionDetailOut` = `SessionOut` + `messages: list[MessageOut]` + `has_more_before: bool` + `oldest_loaded_turn_index: int | None`.
- Produces: `GET /api/chat/{id}` gains an optional `full: bool = False` query param.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_session_loading.py  (append)
from django.contrib.auth.models import User
from django.test import Client

from apps.workspaces.models import WorkspaceMembership


def _api_ctx(n: int):
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="canopy", display_name="Canopy", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    s = Session.objects.create(workspace=ws, created_by=user, title="t")
    for i in range(n):
        Message.objects.create(session=s, turn_index=i, role=Message.USER, plaintext=f"m{i}")
    c = Client()
    c.force_login(user)
    return c, s


def test_get_session_returns_tail_not_full():
    c, s = _api_ctx(50)
    body = c.get(f"/api/chat/{s.id}").json()
    assert len(body["messages"]) == 20
    assert [m["turn_index"] for m in body["messages"]] == list(range(30, 50))
    assert body["has_more_before"] is True
    assert body["oldest_loaded_turn_index"] == 30


def test_get_session_full_returns_everything():
    c, s = _api_ctx(50)
    body = c.get(f"/api/chat/{s.id}?full=true").json()
    assert len(body["messages"]) == 50
    assert body["has_more_before"] is False
    assert body["oldest_loaded_turn_index"] == 0


def test_get_empty_session_cursor_is_null():
    c, s = _api_ctx(0)
    body = c.get(f"/api/chat/{s.id}").json()
    assert body["messages"] == []
    assert body["has_more_before"] is False
    assert body["oldest_loaded_turn_index"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_session_loading.py -k "get_session or empty_session_cursor" -v`
Expected: FAIL — `KeyError: 'has_more_before'` (field not on the schema) / 50 messages returned instead of 20.

- [ ] **Step 3: Add the cursor fields to `SessionDetailOut`**

In `apps/canopy_sessions/schemas.py`:

```python
class SessionDetailOut(SessionOut):
    messages: list[MessageOut]
    # Tail-first cursor: the transcript ships the last N messages by default;
    # these tell the client whether earlier history exists and where the loaded
    # window starts, for scroll-back / "load full". See services.SESSION_TAIL_DEFAULT.
    has_more_before: bool = False
    oldest_loaded_turn_index: int | None = None
```

- [ ] **Step 4: Cap `get_session` to the tail (with `?full=true`)**

Replace `apps/canopy_sessions/api.py::get_session` (`:85-92`) with:

```python
@router.get("/{session_id}", response=SessionDetailOut, summary="Get a session + transcript tail")
def get_session(request: HttpRequest, session_id: uuid.UUID, full: bool = False):
    # Tail-first: never ship the whole transcript by default. The client gets the
    # last SESSION_TAIL_DEFAULT messages + a backward cursor; ?full=true is the
    # explicit escape hatch. Scroll-back pages via GET /{id}/messages?before=.
    session = _session_or_404(request, session_id)
    data = _out(session)
    if full:
        rows, has_more, oldest = services.all_messages(session)
    else:
        rows, has_more, oldest = services.tail_messages(session)
    data["messages"] = [MessageOut.from_orm(m) for m in rows]
    data["has_more_before"] = has_more
    data["oldest_loaded_turn_index"] = oldest
    return data
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_session_loading.py tests/test_chat_api.py -v`
Expected: PASS. (The two pre-existing `test_chat_api.py` cases assert `messages` for 0- and 2-message sessions — both under the tail cap, so unchanged.)

- [ ] **Step 6: Regenerate the OpenAPI types + commit them (CI gate)**

The shape of `SessionDetailOut` changed, so `generated.ts` is now stale and `regen-openapi` CI would fail. Regenerate:

Run (backend up on :8000): `cd frontend && npm run gen:api`
— or dump the schema and use the local variant: `uv run python manage.py export_openapi_schema > frontend/openapi.json 2>/dev/null || curl -s localhost:8000/api/openapi.json > frontend/openapi.json; cd frontend && npm run gen:api:local`

Verify the diff touches only `SessionDetailOut` (adds `has_more_before`, `oldest_loaded_turn_index`) and that the build still typechecks:

Run: `cd frontend && npm run build`
Expected: clean build. `ChatPage` reads only `meta.title`, so the new optional fields don't require any frontend code change.

- [ ] **Step 7: Commit**

```bash
git add apps/canopy_sessions/schemas.py apps/canopy_sessions/api.py frontend/src/api/generated.ts tests/test_session_loading.py
git commit -m "feat(sessions): REST GET /api/chat/{id} is tail-first (+ ?full=true, cursor)"
```

---

### Task 3: Scroll-back endpoint `GET /api/chat/{id}/messages?before=…`

The "Load earlier" backward-pagination path. Cursor-based on `turn_index`; returns the previous window + `has_more_before`. UI wiring is Plan 4 — this task delivers the API + tests. New schema `MessagePageOut` ⇒ regenerate `generated.ts`.

**Files:**
- Modify: `apps/canopy_sessions/schemas.py` (add `MessagePageOut`)
- Modify: `apps/canopy_sessions/api.py` (add the route)
- Modify (regen): `frontend/src/api/generated.ts`
- Test: `tests/test_session_loading.py` (append)

**Interfaces:**
- Consumes: `services.messages_before`, `services.SCROLLBACK_PAGE_DEFAULT`.
- Produces: `MessagePageOut` = `{ messages: list[MessageOut], has_more_before: bool }`.
- Produces: `GET /api/chat/{id}/messages?before=<turn_index>&limit=<N>` → `MessagePageOut`. `before` required (the cursor: return messages strictly older than it); `limit` optional, defaults to `SCROLLBACK_PAGE_DEFAULT`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_session_loading.py  (append)
def test_scrollback_pages_backward_over_rest():
    c, s = _api_ctx(50)
    # First window older than the tail's oldest (30), page of 10
    body = c.get(f"/api/chat/{s.id}/messages?before=30&limit=10").json()
    assert [m["turn_index"] for m in body["messages"]] == list(range(20, 30))
    assert body["has_more_before"] is True
    # Final window reaches the start
    body = c.get(f"/api/chat/{s.id}/messages?before=10&limit=10").json()
    assert [m["turn_index"] for m in body["messages"]] == list(range(0, 10))
    assert body["has_more_before"] is False


def test_scrollback_before_zero_is_empty():
    c, s = _api_ctx(50)
    body = c.get(f"/api/chat/{s.id}/messages?before=0").json()
    assert body["messages"] == []
    assert body["has_more_before"] is False


def test_scrollback_tenant_gated():
    c, s = _api_ctx(5)
    other = User.objects.create_user("no", "no@dimagi.com", "pw")
    ws2 = Workspace.objects.create(slug="other", display_name="Other", created_by=other)
    WorkspaceMembership.objects.create(user=other, workspace=ws2, role=WorkspaceMembership.OWNER)
    c2 = Client(); c2.force_login(other)
    assert c2.get(f"/api/chat/{s.id}/messages?before=5").status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_session_loading.py -k scrollback -v`
Expected: FAIL — 404/route-not-found (the endpoint doesn't exist yet).

- [ ] **Step 3: Add `MessagePageOut`**

In `apps/canopy_sessions/schemas.py`, below `MessageOut`:

```python
class MessagePageOut(Schema):
    """One backward page of transcript for scroll-back ("Load earlier")."""
    messages: list[MessageOut]
    has_more_before: bool
```

- [ ] **Step 4: Add the route**

In `apps/canopy_sessions/api.py`, import `MessagePageOut` in the schema import line, then add below `get_session`:

```python
@router.get(
    "/{session_id}/messages",
    response=MessagePageOut,
    summary="Load earlier transcript (scroll-back)",
)
def list_messages(
    request: HttpRequest,
    session_id: uuid.UUID,
    before: int,
    limit: int = services.SCROLLBACK_PAGE_DEFAULT,
):
    # Cursor-based backward paging: the window of `limit` messages immediately
    # older than `before` (a turn_index), chronological, + whether older exists.
    session = _session_or_404(request, session_id)
    rows, has_more = services.messages_before(session, before=before, limit=limit)
    return {
        "messages": [MessageOut.from_orm(m) for m in rows],
        "has_more_before": has_more,
    }
```

Note the route ordering: `/{session_id}/messages` is a distinct path from `/{session_id}` and `/{session_id}/send`, so Ninja registers it without collision.

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_session_loading.py -v`
Expected: PASS (scroll-back + tenant-gate cases, and the Task 1/2 cases still green).

- [ ] **Step 6: Regenerate the OpenAPI types + commit them (CI gate)**

A new operation + `MessagePageOut` schema were added, so `generated.ts` is stale. Regenerate as in Task 2 Step 6, then:

Run: `cd frontend && npm run build`
Expected: clean build (no frontend consumer yet — Plan 4 wires "Load earlier").

- [ ] **Step 7: Commit**

```bash
git add apps/canopy_sessions/schemas.py apps/canopy_sessions/api.py frontend/src/api/generated.ts tests/test_session_loading.py
git commit -m "feat(sessions): scroll-back endpoint GET /api/chat/{id}/messages?before="
```

---

### Task 4: WS connect snapshot ships the tail, not the head `[:200]`

The `session.state` snapshot the consumer sends on connect must ship the **last** `SESSION_TAIL_DEFAULT` messages (chronological), not `messages[:200]` (the head). Same constant as the REST path; frame shape otherwise unchanged.

**Files:**
- Modify: `apps/canopy_sessions/consumers.py` (`_snapshot`, `:241-256`)
- Test: `tests/test_chat_session_consumer.py` (append) — or `tests/test_session_loading.py` if you prefer the async case co-located; keep it with the consumer tests.

**Interfaces:**
- Consumes: `services.tail_messages`, `services.SESSION_TAIL_DEFAULT`.
- Produces: `session.state.data.messages` is the last N messages, chronological; all other keys unchanged.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_chat_session_consumer.py  (append; reuses _seed/_connect/_recv_match)
from apps.canopy_sessions import services
from apps.canopy_sessions.models import Message


async def test_snapshot_ships_tail_not_head():
    owner, _t, session = await database_sync_to_async(_seed)()

    @database_sync_to_async
    def _fill():
        for i in range(services.SESSION_TAIL_DEFAULT + 15):  # 35 messages
            Message.objects.create(
                session=session, turn_index=i, role=Message.USER, plaintext=f"m{i}",
            )

    await _fill()
    comm = await _connect(session, owner)
    connected, _ = await comm.connect()
    assert connected is True
    snap = await _recv_match(comm, lambda f: f.get("event") == "session.state")
    msgs = snap["data"]["messages"]
    assert len(msgs) == services.SESSION_TAIL_DEFAULT
    # The LAST N, chronological — i.e. the tail, not messages[:200] (the head).
    assert [m["turn_index"] for m in msgs] == list(range(15, 35))
    await comm.disconnect()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_chat_session_consumer.py::test_snapshot_ships_tail_not_head -v`
Expected: FAIL — the snapshot ships `range(0, 20)` (the head slice `[:200]` truncated by the assert) instead of `range(15, 35)`.

- [ ] **Step 3: Ship the tail from `_snapshot`**

In `apps/canopy_sessions/consumers.py`, `_snapshot` (`:241-256`), replace:

```python
        messages = list(self.session.messages.order_by("turn_index")[:200])
```

with:

```python
        # Tail-first: the connect snapshot ships the last N messages (the same
        # SESSION_TAIL_DEFAULT the REST load uses), never the head. Scroll-back
        # for earlier history is REST (GET /{id}/messages?before=); Plan 4 wires
        # it into the panel. The session.state frame shape is otherwise frozen.
        messages, _has_more, _oldest = chat_services.tail_messages(self.session)
```

(`chat_services` is already imported at the top of `consumers.py` as `from . import services as chat_services`.)

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_chat_session_consumer.py -v`
Expected: PASS — the new tail case plus the pre-existing snapshot/canonical/multiplayer cases (none assert a message count, so they stay green).

- [ ] **Step 5: Commit**

```bash
git add apps/canopy_sessions/consumers.py tests/test_chat_session_consumer.py
git commit -m "feat(sessions): WS connect snapshot ships the tail, not the head [:200]"
```

---

### Task 5: Full-suite + boundary regression

A cheap guard task: confirm the whole suite is green, the architecture boundary is intact, and no migration snuck in (this plan is schema/query-only).

**Files:** none (verification only).

- [ ] **Step 1: Full backend suite**

Run: `uv run pytest -q`
Expected: PASS.

- [ ] **Step 2: Boundary + no-pending-migration**

Run: `uv run pytest tests/test_architecture_boundary.py -v && uv run python manage.py makemigrations --check --dry-run`
Expected: boundary PASS; "No changes detected" (Plan 2 adds no model fields — the cursor fields are computed, not stored).

- [ ] **Step 3: Frontend build**

Run: `cd frontend && npm run build`
Expected: clean typecheck + build; `generated.ts` already committed in Tasks 2–3 (a `git status` should show it clean).

- [ ] **Step 4: Commit (if any residual)**

Only if Step 1–3 surfaced a stray fix:

```bash
git add -A
git commit -m "test(sessions): full-suite + boundary green for tail-first loading"
```

---

## Self-Review

**Spec coverage (design §3 — Loading contract, tail-first):**
- REST `GET /api/chat/{id}` no longer uncapped → returns the tail + `has_more_before` + `oldest_loaded_turn_index` → Task 2. ✅
- Back-pagination path for "Load earlier" (cursor-based, previous window + `has_more_before`) → dedicated `GET /api/chat/{id}/messages?before=…` → Task 3. ✅
- Explicit "load full" affordance so nothing is permanently hidden → `?full=true` on the detail endpoint (Task 2, backed by `all_messages`). ✅
- WS connect snapshot ships the tail, not the first 200 → Task 4, `session.state` frame shape preserved. ✅
- Frontend stays working → `ChatPage` reads only `meta.title`; `sessionReducer` replaces state on `session.state`; the new schema fields are additive/optional; build verified in Tasks 2–4. ✅
- Named constant shared by tests + WS + REST → `services.SESSION_TAIL_DEFAULT` (Task 1), referenced by Tasks 2 and 4 and asserted in tests. ✅

**Deferred by design (not gaps):** scroll-back/"load full" UI wiring (Plan 4); per-runner tiered persistence + on-demand runner backfill (Plan 3). Stated explicitly up top.

**Placeholder scan:** No TBD/TODO. Every code step shows real code. The one branch ("dump the schema … or …" in the gen:api steps) is a documented either/or on how to run the regen, not a placeholder.

**Type consistency:** `tail_messages`/`all_messages` return `(messages, has_more_before, oldest_loaded_turn_index)` — the exact three fields the REST handler writes onto `SessionDetailOut`. `messages_before` returns `(messages, has_more_before)` — the exact two fields of `MessagePageOut`. `oldest_loaded_turn_index: int | None` matches the helper returning `None` on an empty session. `before: int` (Task 3) matches `turn_index` (a `PositiveIntegerField`). `SESSION_TAIL_DEFAULT`/`SCROLLBACK_PAGE_DEFAULT` are ints with a single home in `services.py`.

**Independently testable:** Task 1 tests the helpers directly against the DB (no HTTP). Task 2 tests the REST handler (Client). Task 3 tests the scroll-back route (Client). Task 4 tests the WS consumer (WebsocketCommunicator). Each task ships working software: Task 1 adds unused-but-tested helpers; Tasks 2–4 each cap one surface and regenerate/commit types where the wire changed; Task 5 is a green-suite guard. No task depends on a later task's code.

## Notes for the implementer

- **No migration this plan.** If `makemigrations --check` reports a change, something added a model field by mistake — revert it; the cursor is computed.
- **Two gen:api runs** (Tasks 2 and 3). Each must commit `frontend/src/api/generated.ts` in the same commit as its schema change, or `regen-openapi` CI fails the PR (it verifies freshness; it does not auto-commit). Prefer `npm run gen:api` against a live backend; fall back to dumping `openapi.json` + `gen:api:local`.
- **URL prefixes unchanged:** the router still mounts at `/api/chat` and the WS at `ws/chat/{id}/` (renaming those is a later-plan concern) — the SPA keeps working after each task.
- If you'd rather co-locate the async WS test, `tests/test_chat_session_consumer.py` already has the `_seed`/`_connect`/`_recv_match` helpers Task 4 reuses — keep it there rather than duplicating the WS plumbing in `tests/test_session_loading.py`.
