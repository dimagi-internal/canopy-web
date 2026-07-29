# Unified Runner Sessions — Plan 4: Unified Frontend Surface Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Converge the supervisor **Sessions** tab onto ONE unified list. Web-started and runner-discovered sessions are now the same `Session` model (Plans 1–3), so the tab renders a single list where every row opens into the streaming `canopy-ui/chat` `ChatPanel` — tail-first, with "Load earlier" scroll-back, a "Load full session" escape hatch, a running/idle indicator, and a runner-offline / history-unavailable state. The bespoke `OpenSessions` inline "Continue…" box is retired. After this plan a human can, in a browser with the dev stub executor, start a chat with an agent, see it in the unified list, open it into `ChatPanel`, send a message, and watch a streamed markdown reply.

**Architecture:** The list is **backend-unified** — `GET /api/chat/` returns every session the caller can see (their own web sessions ∪ any session in their workspaces that has a `RunnerBinding`), each row carrying computed liveness (`origin`, `running`, `runner_name`, `runner_location`, `session_key`) derived from its binding. One query, one shape, one list component; no client-side merge of two sources. The reusable `ChatSessionsPanel` renders it; `SupervisorPage` drops `OpenSessions` + `ChatSessionsPanel(showList=false)` and mounts the single list. The `canopy-ui/chat` kit gains a pure `prependHistory` helper (+ a `prependMessages` seam on `useSessionSocket`) and an optional `historySlot` on `ChatPanel`; `ChatPage` owns the container wiring — attach-on-open/detach-on-unmount, a cursor seeded from `getSession`, "Load earlier" (Plan 2's `?before=`), "Load full session" (Plan 2 `?full` / Plan 3 backfill), a running/idle chip, and the offline banner. The `/api/chat`→`/api/sessions` URL rename is an isolated, **optional** final task.

**Tech Stack:** React 19 + Vite + TypeScript + Tailwind (frontend); `canopy-ui/chat` reusable kit with vitest unit tests; Django 5 + Django-Ninja + Pydantic v2 (backend, one additive schema change); pytest.

## Global Constraints

- **No backwards compatibility, no data preservation.** Single user; no other consumers. Nothing here needs a compat shim.
- **Framework boundary holds.** `apps/canopy_sessions`, `apps/harness`, `apps/realtime` are framework apps (`tests/test_architecture_boundary.py::FRAMEWORK`); no framework→product imports. `canopy_sessions` importing `apps.harness.models.Runner` is framework→framework (allowed, and the reverse `list_visible_sessions` already imports `RunnerBinding` into `harness`). The boundary test must stay green.
- **WS protocol strings are frozen.** `session.state` and the `chat.stream_*` / `chat.tool_use` / `chat.tool_result` / `chat.stream_error` frame names are the canonical ace-web protocol — unchanged. "Load earlier" is a **REST** path (Plan 2's `GET /api/chat/{id}/messages?before=`), not a new WS frame; it is applied to the socket's `SessionState` in the container via a pure helper, never over the wire.
- **Any backend schema/route change regenerates + commits `frontend/src/api/generated.ts`.** Only Task 2 (and the optional Task 8 rename) change the schema. **The `regen-openapi` CI job fails the PR if `generated.ts` is stale** — Plans 1–3 all hit this gate; it does not auto-commit. Recipe (backend up on :8000): `cd frontend && npm run gen:api`. Offline fallback — dump the schema, then `npm run gen:api:local`:
  ```bash
  uv run python -c "import os,django,json; os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings.test'); django.setup(); from apps.api.api import api; json.dump(api.get_openapi_schema(), open('frontend/openapi.json','w'))"
  cd frontend && npm run gen:api:local
  ```
- **Exact test/build commands** (frontend deps are installed in this worktree):
  - Backend tests: `uv run pytest` (one: `uv run pytest tests/path::name -v`).
  - Frontend typecheck + build: `npm --prefix frontend run build` (`tsc -b && vite build`).
  - Frontend unit tests (kit): `npm --prefix frontend test` runs `vitest run` over `{src,packages}/**/*.{test,spec}.{ts,tsx}`. One file: `cd frontend && npx vitest run packages/canopy-ui/src/chat/history.test.ts`.
- **The stub executor is what makes this browser-verifiable locally.** `CHAT_STUB_EXECUTOR=True` (default in dev; `False` on labs) means a web chat send streams a fake assistant reply over the WS with **no runner** — so the converged surface renders end-to-end without a live laptop. Attaching a web session with no bound runner is a safe no-op: Plan 3's `_set_stream_desired` returns `False` (no binding) and the REST attach returns `{streaming: false}`; nothing errors.
- **This is the LAST plan.** Nothing is deferred to a "Plan 5". Genuinely out-of-scope follow-ups (below) are separate future efforts.

## Deferred / out of scope (future efforts, not this plan)

- **ace-web adoption of `canopy-ui/chat`.** The kit stays framework-generic (§6 of the spec) so ace-web can later drop its parallel chat and speak this protocol — but no ace-web work happens here.
- **Live WS push into the unified LIST.** The list refreshes via REST (mount + a light interval). The existing `supervisor.sessions` WS frame (`useLiveSupervisor().sessions`) becomes unused by the UI after `OpenSessions` is retired; it is left in place (harmless) rather than ripped out. Per-row liveness is still live *inside* `ChatPanel` (the session WS) once a row is opened.
- **Multiplayer co-edit for `origin=runner` sessions.** Presence works for both origins (existing); `Draft` co-edit is web-first (unchanged). Not touched.

## Context (verified against the tree at plan time)

- **Backend session list/detail** — `apps/canopy_sessions/api.py`:
  - `_out(session)` (`:38-47`) → `{id, agent_slug, project, workspace, title, status, created_at}`. Called by `create_session` (`:80`), `list_sessions` (`:93`), `get_session` (`:102`).
  - `list_sessions` (`:83-93`) is **creator-scoped**: `.filter(workspace_id__in=slugs, created_by=request.user)`. Runner-discovered sessions (`_thread_session` creates them with **no** `created_by`) never appear here — they only surface via the harness projection today.
  - `_session_or_404` (`:56-60`) does `Session.objects.select_related("agent")`; `_visible_slugs` (`:50-53`) runs `auto_join_workspaces` then returns pinned-or-all workspace slugs.
  - `SessionOut` (`apps/canopy_sessions/schemas.py:41-47`); `SessionDetailOut` extends it with `messages` + the Plan-2 cursor (`has_more_before`, `oldest_loaded_turn_index`); `MessagePageOut`, `StreamStateOut`, `BackfillStateOut` already exist.
- **The two forked surfaces** — `frontend/src/pages/SupervisorPage.tsx:197-200` (Sessions tab):
  ```tsx
  <ChatSessionsPanel agents={agents ?? undefined} heading="Start a chat" showList={false} />
  <OpenSessions liveSessions={live.sessions} />
  ```
  `OpenSessions` (`frontend/src/components/supervisor/OpenSessions.tsx`) renders `EmdashSessionOut[]` (the harness projection) with an inline "Continue this session…" box that dispatches a repo `Turn` and polls. This whole component is retired.
- **`ChatSessionsPanel`** (`frontend/src/components/chat/ChatSessionsPanel.tsx`) is the reusable, cross-workspace list + "New chat with `<agent>`". Consumed by `ChatListPage` (`/w/:ws/chat`) AND `SupervisorPage`. `showList=false` renders only the "+ New chat" control. Its `ChatSession = SessionOut` rows link to `/w/${s.workspace}/chat/${s.id}`.
- **The chat kit** — `frontend/packages/canopy-ui/src/chat/`:
  - `sessionReducer.ts` (+ `.test.ts`) — pure reducer; `case "session.state": return frame.data` (full replacement). `SessionState = {messages, active_draft, participants, presence_user_ids, current_user_id}` (`protocol.ts`).
  - `useSessionSocket.ts` — owns `state`, connects to `ws/chat/${sessionId}/`, exposes `sendChat/stopChat/updateDraft/...`. No prepend seam yet.
  - `ChatPanel.tsx` — props-in/callbacks-out; renders `MessageList` inside a sticky-bottom scroll container, `SendBox` at the bottom, an optional `banner` above the composer. **No** top-of-list slot yet.
  - `index.ts` exports the kit surface. Existing vitest tests: `sessionReducer.test.ts`, `drafts.test.ts`, `pairToolMessages.test.ts`.
- **`ChatPage`** (`frontend/src/pages/ChatPage.tsx`) — mounts `useSessionSocket({sessionId:id, wsUrl})` + `ChatPanel`; calls `getSession(id)` only for `meta.title`. No attach/detach, no cursor, no load-earlier/full, no running/idle.
- **`api/chat.ts`** — plain-fetch client. `ChatSession = SessionOut`, `ChatSessionDetail = SessionDetailOut`. Has `createSession`, `getSession(id)`, `listSessions()`. No `listMessages`/`attach`/`detach`/`backfill` yet.
- **Attach registry** — `apps/canopy_sessions/attach.py`: `attach`/`detach`/`count`, flat `_TTL = 3600`, **no renewal**. `presence.py` renews TTL on every `touch`. `SessionConsumer.connect` calls `chat_services.attach_session`; `disconnect` calls `detach_session`; the `presence.heartbeat` branch (`consumers.py:65-67`) renews presence but NOT attach.
- **Liveness inputs** — `RunnerBinding` (`apps/canopy_sessions/models.py:162+`) has `runner` (FK, SET_NULL), `session_key`, `last_interacted_at`, `stream_desired`, `backfill_requested`. `Runner` (`apps/harness/models.py:23+`) has `name`, `location` (`LOCAL`/`CLOUD`), `live_status` property (`ONLINE` within a 90s heartbeat window). Reverse accessor `session.runner_binding` raises `RelatedObjectDoesNotExist` (a subclass of `AttributeError`), so `getattr(session, "runner_binding", None)` cleanly yields `None` when absent.
- **No migration in this plan.** Task 2 adds only *computed* schema fields; nothing else touches models.

---

### Task 1: Fix the attach-registry TTL renewal (carry-forward from Plan 3 review)

The flat `_TTL = 3600` in `attach.py` has no renewal path. A session viewed for >1h loses its cache key; a later `detach` then reads `cache.get(key) or 0 == 0`, so the `1→0` edge fires (`detach_session` clears `stream_desired`) even though a viewer is still attached — and in a multi-viewer session it miscounts the detach edge. Mirror `presence.py`: re-touch the attach key on the chat WS heartbeat so the count survives as long as a viewer is connected.

**Files:**
- Modify: `apps/canopy_sessions/attach.py` (add `renew`)
- Modify: `apps/canopy_sessions/consumers.py` (renew on `presence.heartbeat`)
- Test: `tests/test_attach_registry.py` (new); `tests/test_chat_session_consumer.py` (append)

**Interfaces:**
- Produces: `attach.renew(session_id) -> int` — re-writes the current count with a fresh `_TTL`; no-op (returns 0) when nothing is attached.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_attach_registry.py
import pytest
from django.core.cache import cache

from apps.canopy_sessions import attach

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()


def test_renew_preserves_count_and_refreshes():
    sid = "11111111111111111111111111111111"
    attach.attach(sid)
    attach.attach(sid)          # count = 2
    assert attach.renew(sid) == 2
    assert attach.count(sid) == 2   # renewal must NOT change the count, only the TTL


def test_renew_is_noop_when_nothing_attached():
    sid = "22222222222222222222222222222222"
    assert attach.renew(sid) == 0
    assert attach.count(sid) == 0   # renew never resurrects / creates a phantom viewer
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_attach_registry.py -v`
Expected: FAIL — `AttributeError: module 'apps.canopy_sessions.attach' has no attribute 'renew'`.

- [ ] **Step 3: Add `renew` to `attach.py`**

In `apps/canopy_sessions/attach.py`, after `count`:

```python
def renew(session_id) -> int:
    """Re-write the current count with a fresh TTL so a long-lived viewer doesn't
    lose its attach count. Without this the key expires after _TTL and a later
    detach reads 0 -> the 1->0 edge fires and clears stream_desired while a viewer
    is still attached (and miscounts the edge in a multi-viewer session). Mirrors
    presence.touch's renew-on-heartbeat. No-op when nothing is attached."""
    key = _key(session_id)
    n = int(cache.get(key) or 0)
    if n > 0:
        cache.set(key, n, timeout=_TTL)
    return n
```

Update the module docstring's "presence's TTL is the eventual backstop" line to note that the count is now renewed on the chat WS heartbeat (so a connected viewer never loses it).

- [ ] **Step 4: Renew on the chat WS heartbeat**

In `apps/canopy_sessions/consumers.py`, add `attach` to the app imports (beside `from . import services as chat_services` / `presence`):

```python
from . import attach
```

In `receive_json`, the `presence.heartbeat` branch (`:65-67`) becomes:

```python
        if action == "presence.heartbeat":
            await database_sync_to_async(presence.touch)(self.session.id, self.user.id)
            # Keep the attach count alive for as long as the socket is open, so a
            # >1h session doesn't lose it and miscount the detach edge (Plan 4 Task 1).
            await database_sync_to_async(attach.renew)(self.session.id)
            return
```

- [ ] **Step 5: Write the consumer heartbeat test**

```python
# tests/test_chat_session_consumer.py  (append; reuses _seed/_connect/_recv_match)
from apps.canopy_sessions import attach as attach_registry


async def test_heartbeat_renews_attach_count(monkeypatch):
    owner, _t, session = await database_sync_to_async(_seed)()
    renewed = []
    monkeypatch.setattr(attach_registry, "renew", lambda sid: renewed.append(sid) or 1)
    comm = await _connect(session, owner)
    connected, _ = await comm.connect()
    assert connected is True
    await comm.receive_json_from()  # drain the session.state snapshot
    await comm.send_json_to({"action": "presence.heartbeat", "data": {}})
    # Give the consumer a tick to process, then assert renew fired for this session.
    import asyncio
    await asyncio.sleep(0.05)
    assert str(session.id) in [str(s) for s in renewed]
    await comm.disconnect()
```

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/test_attach_registry.py tests/test_chat_session_consumer.py -v`
Expected: PASS (renew semantics + heartbeat-renews + the pre-existing consumer suite still green — the heartbeat branch is additive).

- [ ] **Step 7: Commit**

```bash
git add apps/canopy_sessions/attach.py apps/canopy_sessions/consumers.py tests/test_attach_registry.py tests/test_chat_session_consumer.py
git commit -m "fix(sessions): renew the attach count on the chat WS heartbeat (no premature stream-desired clear)"
```

---

### Task 2: Backend-unify the session list — one query, per-row liveness

**Design decision (backend-unify vs frontend-merge): backend-unify.** The two surfaces today read two different sources — `GET /api/chat/` (creator-scoped `SessionOut`) and the harness projection (`list_visible_sessions` → workspace-scoped `EmdashSessionOut`). Merging them in the frontend would double-count a web session that has a live binding (it appears in both), force reconciliation of two shapes, and push liveness derivation into the client. Instead, `GET /api/chat/` returns **one** deduped list — the caller's own web sessions **∪** any session in their workspaces that has a `RunnerBinding` — with liveness computed on each row from the binding. This is exactly `union(the two current surfaces)` with no phantom rows and one shape the list component renders directly. It adds computed fields to `SessionOut` ⇒ **gen:api regen**.

**Files:**
- Modify: `apps/canopy_sessions/schemas.py` (`SessionOut` — add liveness fields)
- Modify: `apps/canopy_sessions/api.py` (`_out`, `list_sessions`, `_session_or_404` select_related)
- Modify (regen, do not hand-edit): `frontend/src/api/generated.ts`
- Test: `tests/test_session_list_unified.py` (new)

**Interfaces:**
- Produces: `SessionOut` gains `origin: str = "web"`, `running: bool = False`, `runner_name: str | None = None`, `runner_location: str | None = None`, `session_key: str = ""` — all computed, no model change.
- Produces: `services.RUNNING_WINDOW` (a `datetime.timedelta`, 120s) + `services.is_session_running(binding) -> bool`.
- Produces: `GET /api/chat/` returns the deduped union (own web sessions ∪ workspace sessions with a binding), running-first then newest.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_session_list_unified.py
import datetime as dt
import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.canopy_sessions.models import RunnerBinding, Session
from apps.harness.models import Runner
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


def _ctx():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    c = Client(); c.force_login(user)
    return user, ws, c


def test_list_unions_web_and_runner_sessions():
    user, ws, c = _ctx()
    web = Session.objects.create(workspace=ws, created_by=user, origin=Session.ORIGIN_WEB, title="web")
    # a runner-discovered session: no created_by, but it has a binding
    disc = Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER, title="disc")
    runner = Runner.objects.create(name="laptop", workspace=ws, location=Runner.LOCAL,
                                   status=Runner.ONLINE, last_heartbeat_at=timezone.now(), paired_by=user)
    RunnerBinding.objects.create(session=disc, runner=runner, session_key="echo-1",
                                 last_interacted_at=timezone.now())
    body = c.get("/api/chat/").json()
    ids = {row["id"] for row in body}
    assert ids == {str(web.id), str(disc.id)}   # BOTH origins, one row each


def test_list_row_carries_liveness():
    user, ws, c = _ctx()
    disc = Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER, title="disc")
    runner = Runner.objects.create(name="jj-air", workspace=ws, location=Runner.LOCAL,
                                   status=Runner.ONLINE, last_heartbeat_at=timezone.now(), paired_by=user)
    RunnerBinding.objects.create(session=disc, runner=runner, session_key="echo-1",
                                 last_interacted_at=timezone.now())
    row = next(r for r in c.get("/api/chat/").json() if r["id"] == str(disc.id))
    assert row["origin"] == "runner"
    assert row["running"] is True                # runner online + fresh interaction
    assert row["runner_name"] == "jj-air"
    assert row["runner_location"] == "local"
    assert row["session_key"] == "echo-1"


def test_idle_when_runner_offline_or_stale():
    user, ws, c = _ctx()
    disc = Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER, title="disc")
    # runner never heartbeated -> live_status != ONLINE
    runner = Runner.objects.create(name="laptop", workspace=ws, location=Runner.LOCAL,
                                   status=Runner.DISCONNECTED, paired_by=user)
    RunnerBinding.objects.create(session=disc, runner=runner, session_key="echo-1",
                                 last_interacted_at=timezone.now())
    row = next(r for r in c.get("/api/chat/").json() if r["id"] == str(disc.id))
    assert row["running"] is False
    assert row["runner_name"] == "laptop"        # still shown, just not "running"


def test_web_session_without_binding_is_idle():
    user, ws, c = _ctx()
    web = Session.objects.create(workspace=ws, created_by=user, origin=Session.ORIGIN_WEB, title="web")
    row = next(r for r in c.get("/api/chat/").json() if r["id"] == str(web.id))
    assert row["origin"] == "web"
    assert row["running"] is False
    assert row["runner_name"] is None
    assert row["runner_location"] is None


def test_running_sorts_first():
    user, ws, c = _ctx()
    idle = Session.objects.create(workspace=ws, created_by=user, origin=Session.ORIGIN_WEB, title="idle")
    live = Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER, title="live")
    runner = Runner.objects.create(name="laptop", workspace=ws, location=Runner.LOCAL,
                                   status=Runner.ONLINE, last_heartbeat_at=timezone.now(), paired_by=user)
    RunnerBinding.objects.create(session=live, runner=runner, session_key="echo-1",
                                 last_interacted_at=timezone.now())
    body = c.get("/api/chat/").json()
    assert body[0]["id"] == str(live.id)         # running row floats to the top
```

Confirm `WorkspaceMembership.OWNER` / `Runner.ONLINE` / `Runner.DISCONNECTED` constants against the models before running (mirror `tests/test_session_loading.py::_api_ctx` and `tests/test_session_liveness.py`).

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_session_list_unified.py -v`
Expected: FAIL — runner-discovered session missing from the list (creator-scoped) / `KeyError: 'origin'` (fields not on the schema).

- [ ] **Step 3: Add the liveness helper to `services.py`**

In `apps/canopy_sessions/services.py`, below the Plan-2 read helpers:

```python
import datetime as _dt

# A binding is "running" when its runner is live and it was interacted with very
# recently — the same signal OpenSessions derived client-side from the transcript
# tail's freshness, now computed once server-side.
RUNNING_WINDOW = _dt.timedelta(seconds=120)


def is_session_running(binding) -> bool:
    """True when a live runner is actively working this session right now."""
    from apps.harness.models import Runner  # framework->framework; lazy to avoid import cycle

    if binding is None or binding.runner_id is None:
        return False
    if binding.runner.live_status != Runner.ONLINE:
        return False
    ts = binding.last_interacted_at
    return bool(ts and (timezone.now() - ts) <= RUNNING_WINDOW)
```

(`timezone` is already imported in `services.py`.)

- [ ] **Step 4: Enrich `_out` with liveness**

In `apps/canopy_sessions/api.py`, replace `_out` (`:38-47`):

```python
def _out(session: Session) -> dict:
    binding = getattr(session, "runner_binding", None)  # reverse 1:1 -> None when absent
    runner = binding.runner if (binding and binding.runner_id) else None
    return {
        "id": session.id,
        "agent_slug": session.agent.slug if session.agent_id else None,
        "project": session.project,
        "workspace": session.workspace_id,
        "title": session.title,
        "status": session.status,
        "created_at": session.created_at,
        # --- liveness (Plan 4): one shape, computed from the binding ---
        "origin": session.origin,
        "running": services.is_session_running(binding),
        "runner_name": runner.name if runner else None,
        "runner_location": runner.location if runner else None,
        "session_key": binding.session_key if binding else "",
    }
```

Add `origin` + the liveness fields to `SessionOut` in `apps/canopy_sessions/schemas.py`:

```python
class SessionOut(Schema):
    id: uuid.UUID
    agent_slug: str | None
    project: str
    workspace: str
    title: str
    status: str
    created_at: dt.datetime
    # Liveness (Plan 4) — computed from the RunnerBinding; a web session with no
    # binding is origin="web", running=False, runner_name=None.
    origin: str = "web"
    running: bool = False
    runner_name: str | None = None
    runner_location: str | None = None
    session_key: str = ""
```

- [ ] **Step 5: Broaden `list_sessions` to the unified union**

Replace `list_sessions` (`apps/canopy_sessions/api.py:83-93`):

```python
@router.get("/", response=list[SessionOut], summary="List sessions (web + runner-discovered)")
def list_sessions(request: HttpRequest):
    # The ONE unified list (Plan 4): every session the caller can see in their
    # workspaces — their own web sessions UNION any session that has a
    # RunnerBinding (runner-discovered or live). Deduped, running-first, then
    # newest. Replaces the creator-only list + the harness OpenSessions projection.
    from django.db.models import Q

    slugs = _visible_slugs(request)
    rows = (
        Session.objects.select_related("agent", "runner_binding", "runner_binding__runner")
        .filter(workspace_id__in=slugs)
        .filter(Q(created_by=request.user) | Q(runner_binding__isnull=False))
        .distinct()
        .order_by("-created_at")
    )
    out = [_out(s) for s in rows]
    out.sort(key=lambda r: (not r["running"], ))  # stable: running first, created-desc within
    return out
```

Also add `runner_binding` + `runner_binding__runner` to `_session_or_404`'s `select_related` so the detail response computes liveness without an extra query:

```python
def _session_or_404(request: HttpRequest, session_id: uuid.UUID) -> Session:
    session = get_object_or_404(
        Session.objects.select_related("agent", "runner_binding", "runner_binding__runner"),
        pk=session_id,
    )
    if session.workspace_id not in _visible_slugs(request):
        raise HttpError(404, "session not found")
    return session
```

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/test_session_list_unified.py tests/test_chat_api.py tests/test_session_loading.py -v`
Expected: PASS. `test_chat_api.py` asserts only own-session behavior (creates + reads its own sessions) — still in the union — and message counts under the tail cap, so it stays green. `test_session_loading.py`'s detail cases gain optional fields (additive), unaffected.

- [ ] **Step 7: Regenerate the OpenAPI types + commit them (CI gate)**

`SessionOut`'s shape changed, so `generated.ts` is stale (`regen-openapi` would fail). Regenerate:

Run (backend up on :8000): `cd frontend && npm run gen:api`
— or the offline recipe from Global Constraints, then verify the diff touches only `SessionOut` (adds `origin`, `running`, `runner_name`, `runner_location`, `session_key`) and typechecks:

Run: `npm --prefix frontend run build`
Expected: clean build. `ChatSession`/`ChatSessionDetail` (aliases of the generated schema) pick up the fields automatically.

- [ ] **Step 8: Commit**

```bash
git add apps/canopy_sessions/schemas.py apps/canopy_sessions/api.py apps/canopy_sessions/services.py frontend/src/api/generated.ts tests/test_session_list_unified.py
git commit -m "feat(sessions): unified session list — own web + runner-discovered, one shape with per-row liveness"
```

---

### Task 3: One unified list in the Sessions tab — retire `OpenSessions`

Render the single list. `ChatSessionsPanel` (already the reusable list) gains a per-row liveness chip (running/idle + runner name + origin); `SupervisorPage` drops both `ChatSessionsPanel(showList=false)` and `OpenSessions` and mounts one `ChatSessionsPanel` with the list on. `OpenSessions.tsx` is deleted. No backend change ⇒ no gen:api.

**Files:**
- Modify: `frontend/src/components/chat/ChatSessionsPanel.tsx` (liveness chip on each row; light interval refresh)
- Modify: `frontend/src/pages/SupervisorPage.tsx` (mount one list; drop `OpenSessions` + `live.sessions`)
- Delete: `frontend/src/components/supervisor/OpenSessions.tsx`
- Test: `npm --prefix frontend run build` (typecheck); browser render

**Interfaces:**
- Consumes: `ChatSession` (now `SessionOut` with `origin`/`running`/`runner_name`/`runner_location`/`session_key`), `listSessions()`.

- [ ] **Step 1: Add the liveness chip to `ChatSessionsPanel` rows**

In `frontend/src/components/chat/ChatSessionsPanel.tsx`, inside the session `<li>` row (the `<Link>`'s right-hand `<div>`, currently just `relativeTime`), replace the trailing time block with a running/idle chip that keeps the timestamp:

```tsx
                  <div className="flex shrink-0 flex-col items-end gap-0.5 text-xs">
                    {s.running ? (
                      <span className="flex items-center gap-1 font-medium text-success">
                        <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-success" />
                        running
                      </span>
                    ) : (
                      <span className="text-muted-foreground">{relativeTime(s.created_at, now)}</span>
                    )}
                    {s.runner_name && (
                      <span className="text-muted-foreground">
                        {s.runner_name}
                        {s.runner_location ? ` · ${s.runner_location}` : ''}
                      </span>
                    )}
                  </div>
```

The existing sub-label line (`{label} · {s.workspace}`) stays; append the origin when runner-discovered so the two provenances read distinctly:

```tsx
                    <div className="truncate text-xs text-muted-foreground">
                      {label} · {s.workspace}
                      {s.origin === 'runner' ? ' · discovered' : ''}
                      {s.status !== 'active' ? ` · ${s.status}` : ''}
                    </div>
```

Add a light interval refresh so a newly-discovered runner session appears without a reload (only when the list is shown). After the existing mount `useEffect`, add:

```tsx
  // A slow REST refresh keeps the unified list current (the live push into the
  // list is a deferred follow-up; per-row liveness is live inside ChatPanel).
  useEffect(() => {
    if (!showList) return
    const id = window.setInterval(() => {
      listSessions()
        .then(setSessions)
        .catch(() => { /* keep last-good; the mount fetch owns first-error surfacing */ })
    }, 20_000)
    return () => window.clearInterval(id)
  }, [showList])
```

- [ ] **Step 2: Mount one list in the Sessions tab; drop `OpenSessions`**

In `frontend/src/pages/SupervisorPage.tsx`, replace the Sessions `TabsContent` body (`:194-200`):

```tsx
        {/* Sessions — ONE unified list (web-started + runner-discovered are the same
            Session now). Every row opens into the streaming ChatPanel; "New chat with
            <agent> or project" stays as the creation entry point. */}
        <TabsContent value="sessions" className="flex flex-col gap-4">
          <ChatSessionsPanel agents={agents ?? undefined} heading="Sessions" showList />
        </TabsContent>
```

Remove the now-unused `OpenSessions` import (`:11`). Leave `useLiveSupervisor()` and `live` in place (it still drives runner status + waiting counts); only its `.sessions` field is now unused — harmless.

- [ ] **Step 3: Delete `OpenSessions`**

Run: `git rm frontend/src/components/supervisor/OpenSessions.tsx`

Grep for any other importers (there should be none — it was Supervisor-only):
Run: `grep -rn "OpenSessions" frontend/src`
Expected: no matches after the edits.

- [ ] **Step 4: Typecheck + build**

Run: `npm --prefix frontend run build`
Expected: clean build. If `tsc` flags an unused `live`/`liveById` binding in `SupervisorPage` (it is still consumed by the Runners tab + waiting counts), no change needed; only remove a binding `tsc` actually reports unused.

- [ ] **Step 5: Browser render check**

Run the app (`uv run honcho start -f Procfile.dev`, or backend `runserver` + `cd frontend && npm run dev`), open `/supervisor` → **Sessions** tab. Verify: ONE list (no separate "Start a chat" box + live-sessions section), each row shows title + target + a running/idle chip, and "+ New chat" is present. (No `OpenSessions` "Continue…" boxes.)

- [ ] **Step 6: Commit**

```bash
git add frontend/src/components/chat/ChatSessionsPanel.tsx frontend/src/pages/SupervisorPage.tsx
git rm frontend/src/components/supervisor/OpenSessions.tsx
git commit -m "feat(sessions): one unified Sessions list in supervisor; retire OpenSessions"
```

---

### Task 4: Chat kit — `prependHistory` helper + `prependMessages` seam + `ChatPanel.historySlot`

Give the kit the scroll-back primitives, unit-tested without WS plumbing. A pure `prependHistory` merges an older page into `SessionState.messages` (deduped by `turn_index`, chronological); `useSessionSocket` exposes a `prependMessages` seam the container calls after a REST "Load earlier"; `ChatPanel` gains an optional `historySlot` rendered at the top of the scroll container (for the "Load earlier" button + offline banner).

**Files:**
- Create: `frontend/packages/canopy-ui/src/chat/history.ts`
- Create: `frontend/packages/canopy-ui/src/chat/history.test.ts`
- Modify: `frontend/packages/canopy-ui/src/chat/useSessionSocket.ts` (add `prependMessages`)
- Modify: `frontend/packages/canopy-ui/src/chat/ChatPanel.tsx` (add `historySlot`)
- Modify: `frontend/packages/canopy-ui/src/chat/index.ts` (export `prependHistory`)

**Interfaces:**
- Produces: `prependHistory(current: Message[], older: Message[]) => Message[]` — dedupe by `turn_index`, sorted ascending; returns `current` unchanged (same reference) when nothing new prepends.
- Produces: `UseSessionSocketResult.prependMessages(older: Message[]) => void`.
- Produces: `ChatPanelProps.historySlot?: ReactNode`.

- [ ] **Step 1: Write the failing test**

```ts
// frontend/packages/canopy-ui/src/chat/history.test.ts
import { describe, expect, it } from "vitest"
import type { Message } from "./protocol"
import { prependHistory } from "./history"

function msg(turn_index: number, plaintext = `m${turn_index}`): Message {
  return {
    id: `t${turn_index}`, turn_index, role: "user", content: {}, plaintext,
    status: "complete", error_detail: null, started_at: null, completed_at: null,
    created_at: "",
  }
}

describe("prependHistory", () => {
  it("prepends older messages ahead of current, chronological", () => {
    const current = [msg(30), msg(31)]
    const older = [msg(28), msg(29)]
    expect(prependHistory(current, older).map((m) => m.turn_index)).toEqual([28, 29, 30, 31])
  })

  it("dedupes on turn_index (an overlapping page is not double-inserted)", () => {
    const current = [msg(30), msg(31)]
    const older = [msg(29), msg(30)] // 30 overlaps
    expect(prependHistory(current, older).map((m) => m.turn_index)).toEqual([29, 30, 31])
  })

  it("returns the same reference when older is empty", () => {
    const current = [msg(30)]
    expect(prependHistory(current, [])).toBe(current)
  })

  it("returns the same reference when every older row already exists", () => {
    const current = [msg(30), msg(31)]
    expect(prependHistory(current, [msg(30)])).toBe(current)
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run packages/canopy-ui/src/chat/history.test.ts`
Expected: FAIL — cannot resolve `./history`.

- [ ] **Step 3: Write `history.ts`**

```ts
// frontend/packages/canopy-ui/src/chat/history.ts
import type { Message } from "./protocol";

/**
 * Merge an older page (from the REST scroll-back endpoint) into the current
 * transcript: prepend, dedupe by turn_index, keep chronological. Pure — no React,
 * no WebSocket — so the container can apply "Load earlier" to the socket's
 * SessionState without a new WS frame. Returns `current` unchanged (same
 * reference) when nothing new prepends, so callers can skip a re-render.
 */
export function prependHistory(current: Message[], older: Message[]): Message[] {
  if (older.length === 0) return current;
  const seen = new Set(current.map((m) => m.turn_index));
  const fresh = older.filter((m) => !seen.has(m.turn_index));
  if (fresh.length === 0) return current;
  return [...fresh, ...current].sort((a, b) => a.turn_index - b.turn_index);
}
```

- [ ] **Step 4: Run the helper test**

Run: `cd frontend && npx vitest run packages/canopy-ui/src/chat/history.test.ts`
Expected: PASS (all four).

- [ ] **Step 5: Add the `prependMessages` seam to `useSessionSocket`**

In `frontend/packages/canopy-ui/src/chat/useSessionSocket.ts`:
- import: `import { prependHistory } from "./history";` and add `Message` to the `protocol` type import.
- add to `UseSessionSocketResult`: `prependMessages: (older: Message[]) => void;`
- add the callback (near `stopChat`):

```ts
  const prependMessages = useCallback((older: Message[]) => {
    // Apply a REST "Load earlier" page into the live socket state. A later
    // session.state snapshot (e.g. reconnect) resets to the tail — acceptable;
    // the user re-loads earlier if needed.
    setState((prev) => {
      const merged = prependHistory(prev.messages, older);
      return merged === prev.messages ? prev : { ...prev, messages: merged };
    });
  }, []);
```

- add `prependMessages` to the returned object.

- [ ] **Step 6: Add the `historySlot` prop to `ChatPanel`**

In `frontend/packages/canopy-ui/src/chat/ChatPanel.tsx`:
- add to `ChatPanelProps`: `/** Rendered at the top of the scroll container (e.g. a "Load earlier" button / offline banner). */ historySlot?: ReactNode;`
- destructure `historySlot` in the params.
- render it just inside the scroll container, above `MessageList`:

```tsx
      <div ref={containerRef} onScroll={onScroll} className="flex-1 overflow-y-auto">
        {historySlot}
        <MessageList
          messages={state.messages}
          emptyState={emptyState}
          renderMarkdown={renderMarkdown}
        />
      </div>
```

- [ ] **Step 7: Export `prependHistory`**

In `frontend/packages/canopy-ui/src/chat/index.ts`, beside the reducer export:

```ts
export { prependHistory } from "./history";
```

- [ ] **Step 8: Run the kit tests + typecheck**

Run: `npm --prefix frontend test` (all kit unit tests, incl. `history.test.ts`, still green)
Run: `npm --prefix frontend run build`
Expected: PASS + clean build (the new prop/seam are additive; `ChatPage` compiles unchanged since `historySlot`/`prependMessages` are optional/new).

- [ ] **Step 9: Commit**

```bash
git add frontend/packages/canopy-ui/src/chat/history.ts frontend/packages/canopy-ui/src/chat/history.test.ts frontend/packages/canopy-ui/src/chat/useSessionSocket.ts frontend/packages/canopy-ui/src/chat/ChatPanel.tsx frontend/packages/canopy-ui/src/chat/index.ts
git commit -m "feat(chat-kit): prependHistory helper + prependMessages seam + ChatPanel.historySlot"
```

---

### Task 5: `ChatPage` wiring — attach/detach, running/idle, Load earlier, Load full, offline

Wire the container: attach-on-open / detach-on-unmount, a running/idle chip in the header, "Load earlier" (Plan 2 `?before=` → `prependMessages`), "Load full session" (Plan 2 `?full` / Plan 3 backfill), and the runner-offline / history-unavailable banner. The cursor (`has_more_before` + `oldest_loaded_turn_index`) is seeded from `getSession`. No backend change ⇒ no gen:api.

**Attach/detach note (intentional, safe):** `SessionConsumer.connect`/`disconnect` (Plan 3) already attach/detach on the WS lifecycle, and `useSessionSocket` opens that WS on mount. `ChatPage` ALSO calls the REST `attach`/`detach` (this task, per the deliverable) — the two compose safely: the attach registry counts viewers, both paths increment on open and decrement on close symmetrically, the `0↔1` edges are idempotent, and Task 1 keeps the count alive under long sessions. The REST calls add resilience while the WS is (re)connecting; for a web session with no bound runner both are safe no-ops (`{streaming: false}`).

**Files:**
- Modify: `frontend/src/api/chat.ts` (`getSession(id, {full})`, `listMessages`, `attachSession`, `detachSession`, `requestBackfill`; new type aliases)
- Modify: `frontend/src/pages/ChatPage.tsx` (all the wiring)
- Test: `npm --prefix frontend run build` (typecheck); browser E2E

**Interfaces:**
- Consumes: `useSessionSocket().prependMessages`, `ChatPanel.historySlot` (Task 4); `SessionDetailOut.has_more_before`/`oldest_loaded_turn_index` (Plan 2); `MessagePageOut`/`StreamStateOut`/`BackfillStateOut` (Plans 2–3); `SessionOut.running`/`runner_name` (Task 2).
- Produces (client): `getSession(id, opts?)`, `listMessages(id, before, limit?)`, `attachSession(id)`, `detachSession(id)`, `requestBackfill(id)`.

- [ ] **Step 1: Extend the chat client**

In `frontend/src/api/chat.ts`, add type aliases + methods:

```ts
export type MessagePage = components["schemas"]["MessagePageOut"];
export type StreamState = components["schemas"]["StreamStateOut"];
export type BackfillState = components["schemas"]["BackfillStateOut"];

export function getSession(
  id: string,
  opts: { full?: boolean } = {},
): Promise<ChatSessionDetail> {
  const q = opts.full ? "?full=true" : "";
  return request<ChatSessionDetail>(`/api/chat/${encodeURIComponent(id)}${q}`);
}

export function listMessages(
  id: string,
  before: number,
  limit?: number,
): Promise<MessagePage> {
  const q = limit != null ? `&limit=${limit}` : "";
  return request<MessagePage>(`/api/chat/${encodeURIComponent(id)}/messages?before=${before}${q}`);
}

export function attachSession(id: string): Promise<StreamState> {
  return request<StreamState>(`/api/chat/${encodeURIComponent(id)}/attach`, { method: "POST" });
}

export function detachSession(id: string): Promise<StreamState> {
  return request<StreamState>(`/api/chat/${encodeURIComponent(id)}/detach`, { method: "POST" });
}

export function requestBackfill(id: string): Promise<BackfillState> {
  return request<BackfillState>(`/api/chat/${encodeURIComponent(id)}/backfill`, { method: "POST" });
}
```

(Replace the existing single-arg `getSession` with the two-arg version above.)

- [ ] **Step 2: Wire `ChatPage`**

Rewrite `frontend/src/pages/ChatPage.tsx`. The REST `MessageOut` has no `id`/`status`, so map an older page into the kit's `Message` shape (synthetic `id="t<turn_index>"`, `status="complete"`); `prependHistory` dedupes by `turn_index`, so a synthetic-id row never collides with the WS row of the same index.

```tsx
import { useCallback, useEffect, useMemo, useState } from 'react'
import { useParams } from 'react-router-dom'
import { ChatPanel, useSessionSocket, type Message } from 'canopy-ui/chat'
import { Markdown } from '@/components/Markdown'
import { wsUrl } from '@/lib/wsUrl'
import {
  getSession, listMessages, attachSession, detachSession, requestBackfill,
  type ChatSessionDetail,
} from '@/api/chat'

function renderMarkdown(text: string) {
  return <Markdown className="text-sm leading-relaxed">{text}</Markdown>
}

// A REST MessageOut (turn_index/role/plaintext/content/created_at) -> the kit's
// Message shape. Synthetic id + complete status; dedupe is by turn_index.
function restToKitMessage(m: ChatSessionDetail['messages'][number]): Message {
  return {
    id: `t${m.turn_index}`,
    turn_index: m.turn_index,
    role: m.role as Message['role'],
    content: m.content,
    plaintext: m.plaintext,
    status: 'complete',
    error_detail: null,
    started_at: null,
    completed_at: m.created_at,
    created_at: m.created_at,
  }
}

export function ChatPage() {
  const { id = '' } = useParams()
  const [meta, setMeta] = useState<ChatSessionDetail | null>(null)
  const [metaError, setMetaError] = useState<string | null>(null)
  // Scroll-back cursor, seeded from the REST detail (the WS snapshot doesn't
  // carry it — the session.state frame is frozen).
  const [hasMoreBefore, setHasMoreBefore] = useState(false)
  const [oldestTurn, setOldestTurn] = useState<number | null>(null)
  const [loadingEarlier, setLoadingEarlier] = useState(false)
  const [historyUnavailable, setHistoryUnavailable] = useState(false)

  const socket = useSessionSocket({ sessionId: id, wsUrl })

  // Session meta + cursor seed.
  useEffect(() => {
    if (!id) return
    setMeta(null); setMetaError(null); setHistoryUnavailable(false)
    getSession(id)
      .then((m) => {
        setMeta(m)
        setHasMoreBefore(m.has_more_before)
        setOldestTurn(m.oldest_loaded_turn_index)
      })
      .catch((err: unknown) => setMetaError(err instanceof Error ? err.message : 'session not found'))
  }, [id])

  // Attach-on-open / detach-on-unmount (composes with the WS-lifecycle attach;
  // see the task note). Safe no-op for a web session with no bound runner.
  useEffect(() => {
    if (!id) return
    void attachSession(id).catch(() => { /* non-fatal */ })
    return () => { void detachSession(id).catch(() => { /* non-fatal */ }) }
  }, [id])

  const loadEarlier = useCallback(async () => {
    if (oldestTurn == null || loadingEarlier) return
    setLoadingEarlier(true)
    try {
      const page = await listMessages(id, oldestTurn)
      if (page.messages.length > 0) {
        socket.prependMessages(page.messages.map(restToKitMessage))
        setOldestTurn(page.messages[0].turn_index)
      }
      setHasMoreBefore(page.has_more_before)
      // A local (origin=runner) session has no server rows yet: an empty page with
      // nothing older means history lives on the runner -> offer the full backfill.
      if (page.messages.length === 0) setHasMoreBefore(false)
    } catch { /* keep what's shown */ } finally {
      setLoadingEarlier(false)
    }
  }, [id, oldestTurn, loadingEarlier, socket])

  const loadFull = useCallback(async () => {
    setHistoryUnavailable(false)
    try {
      const res = await requestBackfill(id)
      if (res.status === 'unavailable') { setHistoryUnavailable(true); return }
      // ready = already server-full; requested = runner is shipping it. Either way,
      // pull the full transcript (a short delay lets a just-requested backfill land)
      // and prepend it (dedupe by turn_index folds it into the tail).
      if (res.status === 'requested') await new Promise((r) => setTimeout(r, 1200))
      const full = await getSession(id, { full: true })
      socket.prependMessages(full.messages.map(restToKitMessage))
      setHasMoreBefore(false)
      setOldestTurn(full.oldest_loaded_turn_index)
    } catch { setHistoryUnavailable(true) }
  }, [id, socket])

  const emptyState = useMemo(
    () => (
      <div className="flex h-full flex-col items-center justify-center gap-1 p-8 text-center text-sm text-muted-foreground">
        <div className="text-foreground">Start the conversation</div>
        <div className="text-xs">Type a message below to begin.</div>
      </div>
    ),
    [],
  )

  const historySlot = (
    <div className="flex flex-col items-center gap-1 py-2">
      {historyUnavailable && (
        <p className="rounded-md border border-warning/30 bg-warning/10 px-3 py-1.5 text-[12px] text-warning">
          Full history unavailable — runner offline. Showing the latest messages.
        </p>
      )}
      {hasMoreBefore ? (
        <button
          type="button"
          onClick={() => void loadEarlier()}
          disabled={loadingEarlier}
          className="rounded-md border border-border bg-card px-3 py-1 text-[12px] text-foreground-secondary hover:bg-muted disabled:opacity-50"
        >
          {loadingEarlier ? 'Loading…' : 'Load earlier'}
        </button>
      ) : (
        !historyUnavailable && (meta?.origin === 'runner') && socket.state.messages.length > 0 && (
          <button
            type="button"
            onClick={() => void loadFull()}
            className="rounded-md border border-border bg-card px-3 py-1 text-[12px] text-foreground-secondary hover:bg-muted"
          >
            Load full session
          </button>
        )
      )}
    </div>
  )

  const title = meta?.title?.trim() || 'Chat'

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-2 border-b border-border bg-background px-4 py-2">
        <h1 className="truncate text-sm font-semibold text-foreground">{title}</h1>
        {/* Running/idle indicator (from the unified session liveness). */}
        {meta?.running ? (
          <span className="flex shrink-0 items-center gap-1 text-[12px] font-medium text-success">
            <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-success" />
            running{meta.runner_name ? ` · ${meta.runner_name}` : ''}
          </span>
        ) : meta?.runner_name ? (
          <span className="shrink-0 text-[12px] text-muted-foreground">idle · {meta.runner_name}</span>
        ) : null}
        {metaError && <span className="text-xs text-muted-foreground">· {metaError}</span>}
      </div>
      <div className="min-h-0 flex-1">
        <ChatPanel
          state={socket.state}
          connected={socket.connected}
          currentUserId={socket.state.current_user_id}
          onSend={socket.sendChat}
          onStop={socket.stopChat}
          onUpdateDraft={socket.updateDraft}
          onTakeOver={socket.takeOverDraft}
          onDiscard={socket.discardDraft}
          renderMarkdown={renderMarkdown}
          emptyState={emptyState}
          historySlot={historySlot}
        />
      </div>
    </div>
  )
}

export default ChatPage
```

- [ ] **Step 3: Typecheck + build**

Run: `npm --prefix frontend run build`
Expected: clean build. Confirm `success`/`warning` tokens exist in the theme (they do — `docs`/`index.css`); no raw palette literals introduced.

- [ ] **Step 4: Browser E2E (the converged flow)**

Backend with the stub executor (default dev): `uv run honcho start -f Procfile.dev` (or `runserver` + `npm run dev`). Then, logged in:
1. `/supervisor` → **Sessions** → "+ New chat" → pick an agent → lands on `/w/:ws/chat/:id` (ChatPage).
2. Type a message, send → an optimistic user bubble appears, then a **streamed** assistant reply renders as **markdown** (stub executor).
3. Header shows the session title (running/idle chip present if a runner is bound; for the stub web session it's idle/absent — expected).
4. Navigate back to **Sessions** → the new session is in the unified list.

Expected: the whole flow works with no live runner. (Per the "verify frontend render, not curl" learning: render the page in a browser — a stale service-worker bundle can error a new route, which curl would miss.)

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api/chat.ts frontend/src/pages/ChatPage.tsx
git commit -m "feat(chat): ChatPage attach/detach + running-idle + Load earlier/full + runner-offline state"
```

---

### Task 6: Full-suite, boundary, migration & type-freshness guard

A cheap guard: backend suite green, framework boundary intact, no migration pending (this plan adds only computed fields), the kit unit tests green, the frontend builds, and `generated.ts` is fresh (the `regen-openapi` gate).

**Files:** none (verification only).

- [ ] **Step 1: Backend suite**

Run: `uv run pytest -q`
Expected: PASS.

- [ ] **Step 2: Boundary + no-pending-migration**

Run: `uv run pytest tests/test_architecture_boundary.py -v && uv run python manage.py makemigrations --check --dry-run`
Expected: boundary PASS (all new backend code is in `canopy_sessions`; the `apps.harness.models.Runner` import is framework→framework); "No changes detected" (liveness fields are computed, not stored).

- [ ] **Step 3: Kit unit tests + frontend build**

Run: `npm --prefix frontend test && npm --prefix frontend run build`
Expected: PASS (incl. `history.test.ts`, `sessionReducer.test.ts`) + clean typecheck/build.

- [ ] **Step 4: Type freshness (the CI gate)**

Run: `cd frontend && npm run gen:api && git diff --exit-code src/api/generated.ts`
Expected: exit 0 (already committed in Task 2). A non-empty diff means a schema commit was missed — commit it.

- [ ] **Step 5: Commit (only if a residual fix surfaced)**

```bash
git add -A
git commit -m "test(sessions): full-suite + boundary + type-freshness green for the unified surface"
```

---

### Task 7 (OPTIONAL, isolated): URL rename `/api/chat`→`/api/sessions`, `ws/chat/`→`ws/sessions/`

**Skippable — does NOT affect the converged surface or the browser E2E.** A pure rename of the mount prefixes so the URLs match the app rename (`apps/chat`→`apps/canopy_sessions` already shipped in Plan 1; this only renames the externally-visible paths). Do this LAST so a hiccup here can't jeopardize Tasks 1–6. If you skip it, the surface is fully done — the prefix is cosmetic.

**Files:**
- Modify: `apps/api/api.py` (the router mount prefix `/api/chat` → `/api/sessions`)
- Modify: `config/asgi.py` (the WS route `ws/chat/<id>/` → `ws/sessions/<id>/`)
- Modify: `frontend/src/api/chat.ts` (all `/api/chat/...` paths; note the tenant create path `/api/w/${ws}/chat/` → `/api/w/${ws}/sessions/`)
- Modify: `frontend/packages/canopy-ui/src/chat/useSessionSocket.ts` (the `ws/chat/${sessionId}/` path)
- Modify (regen): `frontend/src/api/generated.ts`
- Modify: any test that hard-codes `/api/chat/` or `ws/chat/` (grep below)

**Interfaces:** no schema/shape change — only the path strings move.

- [ ] **Step 1: Find every hard-coded path**

Run: `grep -rn "/api/chat\|ws/chat/\|/api/w/[^)]*chat/" apps frontend config tests`
Expected: a bounded list — the router mount, the ASGI route, `api/chat.ts`, `useSessionSocket.ts`, and the backend tests (`test_chat_api.py`, `test_session_loading.py`, `test_session_liveness.py`, `test_session_backfill.py`, `test_chat_session_consumer.py`).

- [ ] **Step 2: Rename backend mounts**

- In `apps/api/api.py`, change the canopy_sessions router mount prefix from `"chat"`/`/api/chat` to `"sessions"`/`/api/sessions` (leave the router object + operation ids as-is; only the prefix moves). Note the tenant path becomes `/api/w/{ws}/sessions/`.
- In `config/asgi.py`, change the `re_path` for the session consumer from `ws/chat/<session_id>/` to `ws/sessions/<session_id>/`.

- [ ] **Step 3: Update the frontend paths together**

- In `frontend/src/api/chat.ts`, replace every `/api/chat/...` with `/api/sessions/...` and `"/api/w/${input.workspace}/chat/"` with `"/api/w/${input.workspace}/sessions/"`.
- In `frontend/packages/canopy-ui/src/chat/useSessionSocket.ts`, change `wsUrl(\`ws/chat/${sessionId}/\`)` to `wsUrl(\`ws/sessions/${sessionId}/\`)`.

- [ ] **Step 4: Update tests**

Rewrite the `/api/chat/` and `ws/chat/` literals in the grep'd test files to the new prefix. (No assertion logic changes — only the path.)

- [ ] **Step 5: Regen types, run everything**

Run: `cd frontend && npm run gen:api` (paths in `generated.ts` change)
Run: `uv run pytest -q && npm --prefix frontend test && npm --prefix frontend run build`
Expected: all green; `git diff src/api/generated.ts` shows only the path renames.

- [ ] **Step 6: Browser re-verify + commit**

Re-run the Task-5 Step-4 browser flow against the renamed paths (a live session must still connect over `ws/sessions/`), then:

```bash
git add apps/api/api.py config/asgi.py frontend/src/api/chat.ts frontend/packages/canopy-ui/src/chat/useSessionSocket.ts frontend/src/api/generated.ts tests/
git commit -m "refactor(sessions): rename /api/chat->/api/sessions + ws/chat->ws/sessions (isolated)"
```

---

## Self-Review

**Spec coverage (design §5 — the unified surface):**
- **One Sessions list** ("Chat-started and runner-discovered … the same `Session`, so the tab is a single list") → Task 2 (backend-unify: own web ∪ workspace sessions with a binding, deduped) + Task 3 (one `ChatSessionsPanel`; `OpenSessions` deleted). ✅
- **Every row opens into the streaming `ChatPanel`; `OpenSessions` retired** → Task 3 (rows link to `/w/:ws/chat/:id` = ChatPage/ChatPanel; `OpenSessions.tsx` removed). ✅
- **ChatPanel gains tail-first load** → already delivered (Plan 2 WS snapshot ships the tail; the panel renders it). This plan adds the affordances on top. ✅
- **"Load earlier" (scroll-back)** → Task 4 (`prependHistory` + `prependMessages`) + Task 5 (`listMessages(before)` wired into `historySlot`, cursor seeded from `getSession`). ✅
- **Explicit "Load full session"** → Task 5 (`requestBackfill`: `ready`→`?full=true`; `requested`→ delay+`?full=true`; folded via `prependMessages`). ✅
- **Running/idle indicator** → Task 2 (computed `running`/`runner_name`/`runner_location` on `SessionOut`) surfaced in Task 3 (list chip) + Task 5 (ChatPage header chip). ✅
- **Runner-offline / history-unavailable state (tail still shows)** → Task 5 (`requestBackfill` → `unavailable` → warning banner in `historySlot`; the tail keeps rendering). ✅
- **"New chat with `<agent>` or project" stays** → Task 3 keeps `ChatSessionsPanel`'s "+ New chat" dropdown (agents + projects). ✅
- **Attach-on-open / detach-on-unmount** → Task 5 (`attachSession`/`detachSession` on ChatPage mount/unmount), composing safely with the Plan-3 WS-lifecycle attach (documented; balanced edges; Task 1 keeps the count alive). ✅
- **Carry-forward: attach-TTL renewal** → Task 1 (`attach.renew` on the chat WS heartbeat). ✅

**Unified-list decision:** backend-unify (one query, one shape, per-row liveness) — rationale in Task 2's header: frontend-merge would double-count web-sessions-with-a-binding, reconcile two shapes (`SessionOut` vs `EmdashSessionOut`), and push liveness derivation to the client. Backend-unify is the spec's "same `Session`, one list."

**URL rename:** **included as an OPTIONAL, isolated final task (Task 7)** — not required by the E2E, sequenced last, its own commit, skippable without touching the converged surface. The pure-rename risk (a hard-coded path) is bounded by the Step-1 grep.

**Test-surface map (each task independently verifiable in this repo):**
- pytest: Task 1 (attach renew + consumer heartbeat), Task 2 (unified list + liveness), Task 6 (full suite/boundary/migration).
- vitest: Task 4 (`prependHistory`), Task 6 (kit suite).
- build (typecheck): Tasks 2, 3, 4, 5, 6 (and 7).
- browser: Task 3 (one list renders), Task 5 (the full converged chat flow — start → list → open → send → streamed markdown reply), Task 7 (re-verify after rename).
No task's only verification is "deploy and watch." The E2E-relevant flow is fully wired by the end of Task 5 (Tasks 6–7 are guard/optional).

**Placeholder scan:** no TBD/TODO. Every code step shows real code grounded in read signatures (`_out`/`list_sessions`/`SessionOut`; `useSessionSocket`/`ChatPanel`/`sessionReducer`; `attach.py`/`presence.py`/`consumers.py`). Constants (`RUNNING_WINDOW=120s`, tail defaults) are named, not magic.

**Type consistency:**
- `SessionOut` liveness fields (`origin`, `running`, `runner_name`, `runner_location`, `session_key`) are written by `_out` (Task 2) exactly as declared, and consumed unchanged in `ChatSessionsPanel` (Task 3) + `ChatPage` (Task 5) via the generated `ChatSession`/`ChatSessionDetail` aliases.
- `prependHistory(current, older)` / `prependMessages(older)` (Task 4) take/return kit `Message[]`; `restToKitMessage` (Task 5) produces that exact shape (synthetic `id`, `status:"complete"`), and dedup is by `turn_index` so a synthetic id never collides with a WS row.
- The cursor fields `has_more_before` / `oldest_loaded_turn_index` (Plan 2, on `SessionDetailOut`) seed ChatPage state and match `MessagePageOut.has_more_before`; `listMessages(before: number)` matches `turn_index` (a `PositiveIntegerField`).
- `attachSession`/`detachSession`/`requestBackfill` return `StreamStateOut`/`BackfillStateOut` (Plan 3) — read as `{streaming}` / `{status}` in ChatPage.

## Notes for the implementer

- **No migration this plan.** If `makemigrations --check` reports a change, a model field was added by mistake — the liveness fields are computed in `_out`, not stored. Revert.
- **One `gen:api` run (Task 2)** — commit `frontend/src/api/generated.ts` in the same commit as the `SessionOut` change, or `regen-openapi` CI fails the PR. Task 7 (optional) regenerates again (path renames). Tasks 3/4/5 are frontend-only — confirm `git status` shows no `frontend/src/api/generated.ts` diff there.
- **The WS `session.state` frame is frozen** (Plan 2) — it does NOT carry the cursor, so ChatPage seeds `has_more_before`/`oldest_loaded_turn_index` from the REST `getSession`. A reconnect resets the socket to the tail (the reducer replaces state on `session.state`); the user re-loads earlier — acceptable, and the E2E's short sessions never hit it.
- **Attach double-path is intentional and safe** — the WS consumer (Plan 3) and ChatPage REST (Task 5) both attach/detach; balanced edges + Task 1's heartbeat renewal keep the count correct. Do not "fix" it by ripping the consumer attach (that would regress Plan 3's tests and the spec's "attach = WS join").
- **`live.sessions` is now unused** after Task 3 — left in `useLiveSupervisor` on purpose (the live push into the list is a stated follow-up). Only remove a `SupervisorPage` binding that `tsc` actually reports unused.
- **Verify in the browser, not curl** — this is a PWA; a stale service-worker bundle can error a newly-wired route that curl (which bypasses the SW) would never catch. Render `/supervisor` → Sessions → a live chat.
- **Runner streaming (Plans 3/5) needs a live runner** to exercise the running-chip / stream-on-attach against a REAL laptop — that is an operational check AFTER this plan's green suite, not a task here; the stub executor makes the converged surface fully browser-verifiable without one.
```
