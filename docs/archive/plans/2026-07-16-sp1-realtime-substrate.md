# SP1 — Realtime Substrate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give canopy-web its first realtime transport (Django Channels + Redis) and put two surfaces live over it — the per-turn `TurnEvent` ledger tail and `/supervisor` — with no new domain model.

**Architecture:** A new framework app `apps/realtime` holds two `AsyncJsonWebsocketConsumer`s. The write path stays untouched except for one harness-owned signal (needed because `append_events` uses `bulk_create`, which skips `post_save`). Fanout mirrors `apps/push`: signal/`post_save` → `transaction.on_commit` → `channel_layer.group_send`. The turn tail replays via the existing `?after=seq` cursor then live-tails; `/supervisor` sends a snapshot then deltas. `config/asgi.py`'s Starlette router gains a Channels `ProtocolTypeRouter` for the `websocket` scope.

**Tech Stack:** Django 5 ASGI, `channels`, `channels_redis`, uvicorn, Redis (ElastiCache in prod, in-memory layer in dev/tests), React 19 + Vite (vitest), plain browser `WebSocket`.

## Global Constraints

- Framework/product boundary: `apps/realtime` is **framework**; may import only framework apps (`harness`, `agents`, `workspaces`, `tokens`, `push`, `common`); never a product app. Add it to `FRAMEWORK` in `tests/test_architecture_boundary.py` and the `ARCHITECTURE.md` tier table.
- Realtime is an enhancement, never a hard dependency: a missing/misconfigured channel layer must degrade to a no-op, never raise in a write path.
- WS auth reads `settings.SESSION_COOKIE_NAME` (it is `sessionid_canopy` on connectlabs, `sessionid` elsewhere) — never hardcode the cookie name.
- Respect the `/canopy` `FORCE_SCRIPT_NAME` prefix; `config/asgi_prefix.py::StripScriptName` already strips it for `websocket` scopes.
- Dependency floors: `channels>=4.1,<5.0`, `channels-redis>=4.2,<5.0`.
- Python deps via `uv` (uv.lock committed); backend tests `uv run pytest`; frontend `cd frontend && npm run build` / `npx vitest`.

---

### Task 1: Scaffold the `realtime` app + Channels deps + settings

**Files:**
- Modify: `pyproject.toml` (add `channels`, `channels-redis`)
- Create: `apps/realtime/__init__.py`, `apps/realtime/apps.py`
- Modify: `config/settings/base.py` (INSTALLED_APPS + `CHANNEL_LAYERS`)
- Modify: `config/settings/connectlabs.py` (Redis channel layer)
- Modify: `tests/test_architecture_boundary.py` (add `realtime` to FRAMEWORK)
- Modify: `ARCHITECTURE.md` (tier table)
- Test: `tests/test_architecture_boundary.py` (existing), `apps/realtime/tests/test_app.py`

**Interfaces:**
- Produces: the `apps.realtime` app label; `CHANNEL_LAYERS["default"]` configured (InMemory when no `REDIS_URL`, `RedisChannelLayer` when set).

- [ ] **Step 1:** Add deps to `pyproject.toml` `dependencies` list:
```toml
    "channels>=4.1,<5.0",
    "channels-redis>=4.2,<5.0",
```
- [ ] **Step 2:** `uv sync --extra dev` (updates uv.lock). Expected: resolves channels + channels-redis.
- [ ] **Step 3:** Create `apps/realtime/__init__.py` (empty) and `apps/realtime/apps.py`:
```python
from django.apps import AppConfig


class RealtimeConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.realtime"
    label = "realtime"

    def ready(self) -> None:
        # Connect fanout receivers (import for side effect). Kept in ready() so
        # signal wiring happens exactly once, after the app registry is populated.
        from . import signals  # noqa: F401
```
(Task 4 creates `signals.py`; until then, add a temporary empty `apps/realtime/signals.py` so `ready()` imports cleanly.)
- [ ] **Step 4:** Add `"apps.realtime",` to `INSTALLED_APPS` in `config/settings/base.py` (after `"apps.harness",`). **Channels ordering note:** `channels` must be listed too — add `"channels",` to INSTALLED_APPS *above* the Local apps block (Channels needs to be installed for `ASGI_APPLICATION`/runworker, though we drive ASGI via Starlette).
- [ ] **Step 5:** Add to `config/settings/base.py` after `ASGI_APPLICATION`:
```python
# --- Channels realtime layer (apps/realtime) -------------------------
# In-memory by default so a fresh checkout / single-process runserver works with
# zero infra. When REDIS_URL is set (docker-compose, prod) use the Redis layer so
# fan-out crosses processes/containers. Tests force InMemory (see test settings).
_CHANNELS_REDIS_URL = env("REDIS_URL", default="")
if _CHANNELS_REDIS_URL:
    CHANNEL_LAYERS = {
        "default": {
            "BACKEND": "channels_redis.core.RedisChannelLayer",
            "CONFIG": {"hosts": [_CHANNELS_REDIS_URL], "prefix": "canopy:realtime:"},
        }
    }
else:
    CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}
```
- [ ] **Step 6:** In `config/settings/connectlabs.py`, replace the "Channels layer lands with W4" comment block's tail by adding, inside the `if _REDIS_URL:` block after `CACHES = {...}`:
```python
    CHANNEL_LAYERS = {
        "default": {
            "BACKEND": "channels_redis.core.RedisChannelLayer",
            # Dedicated prefix so channel-layer pub/sub never collides with the
            # Django cache on the same shared ElastiCache instance.
            "CONFIG": {"hosts": [_REDIS_URL], "prefix": "canopy:realtime:"},
        }
    }
```
- [ ] **Step 7:** In `tests/test_architecture_boundary.py`, add `"realtime"` to the `FRAMEWORK` set.
- [ ] **Step 8:** In `ARCHITECTURE.md`, add a `realtime` row to the framework tier table (one line: "realtime — Channels WS transport; pushes the harness ledger + supervisor live").
- [ ] **Step 9:** Create `apps/realtime/tests/__init__.py` and `apps/realtime/tests/test_app.py`:
```python
from django.apps import apps


def test_realtime_app_is_installed():
    assert apps.is_installed("apps.realtime")


def test_channel_layers_configured(settings):
    assert "default" in settings.CHANNEL_LAYERS
```
- [ ] **Step 10:** Run `uv run pytest tests/test_architecture_boundary.py apps/realtime/tests/test_app.py -q`. Expected: PASS.
- [ ] **Step 11:** Commit: `feat(realtime): scaffold framework app + channels deps + channel layer`

---

### Task 2: `groups.py` — group names, membership gates, null-safe publish, serialization

**Files:**
- Create: `apps/realtime/groups.py`
- Test: `apps/realtime/tests/test_groups.py`

**Interfaces:**
- Produces:
  - `turn_group(turn_id: uuid.UUID | str) -> str`
  - `supervisor_user_group(user_id: int) -> str`
  - `user_can_read_turn(user, turn: Turn) -> bool`
  - `serialize_turn_event(te: TurnEvent) -> dict` → `{"seq", "kind", "payload", "ts"}`
  - `publish(group: str, message: dict) -> None` (null-safe; no-op if no channel layer)

- [ ] **Step 1:** Write `apps/realtime/tests/test_groups.py`:
```python
import uuid
import pytest
from apps.realtime import groups


def test_turn_group_is_stable():
    tid = uuid.uuid4()
    assert groups.turn_group(tid) == f"turn.{tid.hex}"
    assert groups.turn_group(str(tid)) == groups.turn_group(tid)


def test_supervisor_user_group():
    assert groups.supervisor_user_group(7) == "supervisor.user.7"


@pytest.mark.django_db
def test_user_can_read_turn_by_workspace(agent_factory, user_factory, turn_factory, membership_factory):
    ws_user = user_factory()
    agent = agent_factory()  # belongs to a workspace
    membership_factory(user=ws_user, workspace=agent.workspace)
    turn = turn_factory(agent=agent)
    assert groups.user_can_read_turn(ws_user, turn) is True
    assert groups.user_can_read_turn(user_factory(), turn) is False


def test_publish_is_noop_without_layer(settings):
    settings.CHANNEL_LAYERS = {}
    # must not raise
    groups.publish("turn.x", {"type": "turn.event", "seq": 1})
```
(If those factories don't exist, use inline ORM creation in the test — see existing `apps/harness/tests` conftest for fixtures; reuse them.)
- [ ] **Step 2:** Run the test → FAIL (module missing).
- [ ] **Step 3:** Write `apps/realtime/groups.py`:
```python
"""Group names, membership gates, and a null-safe publish helper.

Pure functions (no socket, no async) so they unit-test without Channels. The
one impure helper, publish(), degrades to a no-op when no channel layer is
configured — realtime is an enhancement, never a hard dependency of a write.
"""
from __future__ import annotations

import logging
import uuid

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from apps.harness.models import Turn, TurnEvent
from apps.workspaces.services import user_workspace_slugs

log = logging.getLogger(__name__)


def turn_group(turn_id: uuid.UUID | str) -> str:
    hexid = turn_id.hex if isinstance(turn_id, uuid.UUID) else uuid.UUID(str(turn_id)).hex
    return f"turn.{hexid}"


def supervisor_user_group(user_id: int) -> str:
    return f"supervisor.user.{user_id}"


def turn_workspace_slug(turn: Turn) -> str | None:
    if turn.agent_id:
        return turn.agent.workspace.slug if turn.agent.workspace_id else None
    return turn.workspace.slug if turn.workspace_id else None


def user_can_read_turn(user, turn: Turn) -> bool:
    if not getattr(user, "is_authenticated", False):
        return False
    if user.is_superuser:
        return True
    slug = turn_workspace_slug(turn)
    return bool(slug) and slug in user_workspace_slugs(user)


def serialize_turn_event(te: TurnEvent) -> dict:
    return {"seq": te.seq, "kind": te.kind, "payload": te.payload, "ts": te.ts.isoformat()}


def publish(group: str, message: dict) -> None:
    layer = get_channel_layer()
    if layer is None:
        return
    try:
        async_to_sync(layer.group_send)(group, message)
    except Exception:  # pragma: no cover - realtime must never break a write
        log.exception("realtime publish to %s failed", group)
```
- [ ] **Step 4:** Run `uv run pytest apps/realtime/tests/test_groups.py -q`. Expected: PASS.
- [ ] **Step 5:** Commit: `feat(realtime): group names, membership gate, null-safe publish`

---

### Task 3: harness `turn_events_appended` signal fired from `append_events`

**Files:**
- Create: `apps/harness/signals.py`
- Modify: `apps/harness/services.py` (`append_events`)
- Test: `apps/harness/tests/test_events_signal.py`

**Interfaces:**
- Produces: `apps.harness.signals.turn_events_appended` — a `django.dispatch.Signal` sent with `sender=Turn, turn=<Turn>, rows=<list[TurnEvent]>` **after commit**.

- [ ] **Step 1:** Write `apps/harness/tests/test_events_signal.py`:
```python
import pytest
from django.db import transaction
from apps.harness import services
from apps.harness.signals import turn_events_appended


@pytest.mark.django_db
def test_append_events_fires_signal_after_commit(turn_factory):
    turn = turn_factory()
    received = []
    turn_events_appended.connect(lambda **kw: received.append(kw), weak=False)
    services.append_events(turn, [{"kind": "assistant", "payload": {"text": "hi"}}])
    assert len(received) == 1
    assert received[0]["turn"].pk == turn.pk
    assert [r.seq for r in received[0]["rows"]] == [1]
```
- [ ] **Step 2:** Run → FAIL (no `signals` module).
- [ ] **Step 3:** Create `apps/harness/signals.py`:
```python
"""Domain signals emitted by the harness write path.

`turn_events_appended` fires AFTER the append commits (via transaction.on_commit)
so a subscriber (apps/realtime) can fan out durable events without racing the DB.
It exists because append_events uses bulk_create, which does NOT emit post_save —
a post_save receiver on TurnEvent would silently never fire.
"""
from django.dispatch import Signal

# providing_args (informational): turn: Turn, rows: list[TurnEvent]
turn_events_appended = Signal()
```
- [ ] **Step 4:** In `apps/harness/services.py`, modify `append_events` to fire the signal on commit. Replace the function body's tail:
```python
def append_events(turn: Turn, events: list[dict]) -> int:
    with transaction.atomic():
        Turn.objects.select_for_update().get(pk=turn.pk)
        current = (
            TurnEvent.objects.filter(turn=turn).aggregate(m=Max("seq"))["m"] or 0
        )
        rows = [
            TurnEvent(turn=turn, seq=current + i + 1, kind=e["kind"], payload=e.get("payload", {}))
            for i, e in enumerate(events)
        ]
        TurnEvent.objects.bulk_create(rows)

    def _fire():
        from apps.harness.signals import turn_events_appended
        turn_events_appended.send(sender=Turn, turn=turn, rows=rows)

    transaction.on_commit(_fire)
    return len(rows)
```
(Import stays local to avoid import-time cycles.)
- [ ] **Step 5:** Run `uv run pytest apps/harness/tests/test_events_signal.py -q`. Expected: PASS. Then run the full harness suite `uv run pytest apps/harness -q` to confirm no regression (append_events is on a hot path).
- [ ] **Step 6:** Commit: `feat(harness): emit turn_events_appended signal on commit`

---

### Task 4: `realtime/signals.py` — the three fanout receivers

**Files:**
- Modify: `apps/realtime/signals.py` (replace the temporary empty file)
- Test: `apps/realtime/tests/test_fanout.py`

**Interfaces:**
- Consumes: `apps.harness.signals.turn_events_appended`, `post_save` on `apps.harness.models.Runner`, `post_save` on `apps.push.models.AgentWaitingSnapshot`; `groups.publish`, `groups.turn_group`, `groups.supervisor_user_group`, `groups.serialize_turn_event`.
- Produces: live frames `{"type": "turn.event", ...}`, `{"type": "supervisor.runner", ...}`, `{"type": "supervisor.waiting", ...}` on the right groups.

- [ ] **Step 1:** Write `apps/realtime/tests/test_fanout.py` (uses InMemory layer + `capture_on_commit_callbacks`):
```python
import pytest
from django.test import TestCase
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync
from apps.harness import services
from apps.realtime import groups


@pytest.mark.django_db(transaction=True)
def test_turn_event_fanout(turn_factory, django_capture_on_commit_callbacks):
    turn = turn_factory()
    layer = get_channel_layer()
    async_to_sync(layer.group_add)(groups.turn_group(turn.id), "test-chan")
    with django_capture_on_commit_callbacks(execute=True):
        services.append_events(turn, [{"kind": "assistant", "payload": {"text": "hi"}}])
    msg = async_to_sync(layer.receive)("test-chan")
    assert msg["type"] == "turn.event"
    assert msg["event"]["seq"] == 1
```
(Add analogous `test_runner_fanout` saving a Runner and asserting a `supervisor.runner` frame on `supervisor_user_group(runner.paired_by_id)`, and `test_waiting_fanout` by calling `apps.push.services.refresh_agent_waiting`.)
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Write `apps/realtime/signals.py`:
```python
"""Fan-out receivers: turn the harness write path into live WS frames.

Mirrors apps/push/signals.py — post_save/custom-signal → transaction.on_commit →
group_send. Every publish is wrapped so a realtime failure never breaks a write.
"""
from __future__ import annotations

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.harness.models import Runner
from apps.harness.signals import turn_events_appended
from apps.push.models import AgentWaitingSnapshot
from apps.workspaces.services import workspace_member_ids
from . import groups


@receiver(turn_events_appended, dispatch_uid="realtime_turn_events")
def _on_turn_events(sender, turn, rows, **kwargs):
    payload_group = groups.turn_group(turn.id)
    events = [groups.serialize_turn_event(r) for r in rows]
    # already inside on_commit (append_events fires post-commit); publish directly.
    for ev in events:
        groups.publish(payload_group, {"type": "turn.event", "event": ev})


@receiver(post_save, sender=Runner, dispatch_uid="realtime_runner")
def _on_runner_saved(sender, instance: Runner, **kwargs):
    if not instance.paired_by_id:
        return
    frame = {
        "type": "supervisor.runner",
        "runner": {
            "id": str(instance.id),
            "name": instance.name,
            "kind": instance.kind,
            "status": instance.live_status,
            "last_heartbeat_at": instance.last_heartbeat_at.isoformat() if instance.last_heartbeat_at else None,
        },
    }
    grp = groups.supervisor_user_group(instance.paired_by_id)
    transaction.on_commit(lambda: groups.publish(grp, frame))


@receiver(post_save, sender=AgentWaitingSnapshot, dispatch_uid="realtime_waiting")
def _on_waiting_saved(sender, instance: AgentWaitingSnapshot, **kwargs):
    agent = instance.agent
    if not agent.workspace_id:
        return
    frame = {
        "type": "supervisor.waiting",
        "agent": agent.slug,
        "waiting_count": instance.waiting_count,
    }
    user_ids = list(workspace_member_ids(agent.workspace))
    def _fire():
        for uid in user_ids:
            groups.publish(groups.supervisor_user_group(uid), frame)
    transaction.on_commit(_fire)
```
- [ ] **Step 4:** Add `workspace_member_ids(ws) -> list[int]` to `apps/workspaces/services.py` if it doesn't exist:
```python
def workspace_member_ids(ws) -> list[int]:
    return list(ws.memberships.values_list("user_id", flat=True))
```
(Check the membership related_name first; adjust `.memberships` to the actual accessor.)
- [ ] **Step 5:** Run `uv run pytest apps/realtime/tests/test_fanout.py -q`. Expected: PASS.
- [ ] **Step 6:** Commit: `feat(realtime): fan-out receivers for turn events + supervisor`

---

### Task 5: `channels_auth.py` — cookie-then-Bearer handshake middleware

**Files:**
- Create: `apps/realtime/channels_auth.py`
- Test: `apps/realtime/tests/test_channels_auth.py`

**Interfaces:**
- Produces: `RealtimeAuthMiddleware(app)` — an ASGI middleware that sets `scope["user"]` (a real `User` or `AnonymousUser`) from the session cookie first, then `Authorization: Bearer <PAT>`.

- [ ] **Step 1:** Write `apps/realtime/tests/test_channels_auth.py` — connect a `WebsocketCommunicator` through the middleware wrapping a probe consumer that echoes `scope["user"].is_authenticated`; assert anon by default, authed with a valid session cookie, authed with a valid Bearer PAT. (See existing token tests in `apps/tokens/tests` for how to mint a PAT.)
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Write `apps/realtime/channels_auth.py`:
```python
"""WebSocket handshake auth: session cookie first, then Bearer PAT.

Ports ace-web's AceSessionAuthMiddleware. Reads settings.SESSION_COOKIE_NAME
(sessionid_canopy on connectlabs) — never hardcoded. Bearer resolution reuses
apps/tokens so scripted clients (and SP4's ace-web) authenticate the same way
they do over REST.
"""
from __future__ import annotations

from http.cookies import SimpleCookie

from asgiref.sync import sync_to_async
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from importlib import import_module


def _header(scope, name: bytes) -> bytes | None:
    for k, v in scope.get("headers", []):
        if k == name:
            return v
    return None


@sync_to_async
def _user_from_session(scope):
    raw = _header(scope, b"cookie")
    if not raw:
        return None
    cookies = SimpleCookie()
    cookies.load(raw.decode("latin1"))
    morsel = cookies.get(settings.SESSION_COOKIE_NAME)
    if not morsel:
        return None
    engine = import_module(settings.SESSION_ENGINE)
    session = engine.SessionStore(morsel.value)
    uid = session.get("_auth_user_id")
    if not uid:
        return None
    User = get_user_model()
    return User.objects.filter(pk=uid, is_active=True).first()


@sync_to_async
def _user_from_bearer(scope):
    raw = _header(scope, b"authorization")
    if not raw or not raw.lower().startswith(b"bearer "):
        return None
    token = raw[7:].decode("latin1").strip()
    from apps.tokens.services import resolve_token  # returns User | None
    return resolve_token(token)


class RealtimeAuthMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        user = await _user_from_session(scope) or await _user_from_bearer(scope)
        scope = dict(scope)
        scope["user"] = user or AnonymousUser()
        return await self.app(scope, receive, send)
```
(Verify the token resolver: it's whatever `apps/tokens/middleware.py::BearerTokenAuthMiddleware` calls — reuse that exact function; adjust the import to match.)
- [ ] **Step 4:** Run `uv run pytest apps/realtime/tests/test_channels_auth.py -q`. Expected: PASS.
- [ ] **Step 5:** Commit: `feat(realtime): cookie-then-Bearer WS handshake auth`

---

### Task 6: `TurnConsumer` — per-turn ledger tail

**Files:**
- Create: `apps/realtime/consumers.py` (TurnConsumer)
- Test: `apps/realtime/tests/test_turn_consumer.py`

**Interfaces:**
- Consumes: `groups.turn_group`, `groups.user_can_read_turn`, `groups.serialize_turn_event`, the harness `Turn` model + `?after=seq` read.
- Produces: WS frames — on connect, replayed `{"event": {seq,kind,payload,ts}}` then live `turn.event` frames. Close codes: `4001` anon, `4003` forbidden, `4004` turn not found.

- [ ] **Step 1:** Write `apps/realtime/tests/test_turn_consumer.py`:
```python
import uuid
import pytest
from channels.testing import WebsocketCommunicator
from apps.realtime.consumers import TurnConsumer
from apps.harness import services


def _app():
    return TurnConsumer.as_asgi()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_anon_rejected(user_factory, turn_factory):
    turn = await sync_turn(turn_factory)
    comm = WebsocketCommunicator(_app(), f"/ws/turns/{turn.id}/")
    comm.scope["user"] = AnonymousUser()
    comm.scope["url_route"] = {"kwargs": {"turn_id": str(turn.id)}}
    connected, code = await comm.connect()
    assert not connected
```
(Fill in helpers; assert: non-member → 4003; member → receives replay of a pre-existing event then a live event appended via `services.append_events` after connect. Set `comm.scope["user"]` and `comm.scope["url_route"]` manually since we bypass the router in unit tests.)
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Write `TurnConsumer` in `apps/realtime/consumers.py`:
```python
"""WebSocket consumers over the realtime transport.

TurnConsumer: live-tail one turn's append-only TurnEvent ledger. Replay is
cursor-based (?after=seq) reusing the same read the REST endpoint uses, then
live frames arrive via the turn.{id} group. Idempotent on the client by seq.
"""
from __future__ import annotations

import uuid

from asgiref.sync import sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer

from apps.harness.models import Turn
from . import groups


class TurnConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        user = self.scope.get("user")
        if not getattr(user, "is_authenticated", False):
            await self.close(code=4001)
            return
        raw_id = self.scope["url_route"]["kwargs"]["turn_id"]
        turn = await self._get_turn(raw_id)
        if turn is None:
            await self.close(code=4004)
            return
        if not await sync_to_async(groups.user_can_read_turn)(user, turn):
            await self.close(code=4003)
            return
        self.turn = turn
        self.group = groups.turn_group(turn.id)
        await self.channel_layer.group_add(self.group, self.channel_name)
        await self.accept()
        # Cursor replay: ?after=<seq> (default 0), then live-tail.
        after = self._after_from_query()
        for ev in await self._replay(turn, after):
            await self.send_json({"event": ev})

    async def disconnect(self, code):
        grp = getattr(self, "group", None)
        if grp:
            await self.channel_layer.group_discard(grp, self.channel_name)

    async def turn_event(self, message):
        # group_send type="turn.event" -> this handler. Forward the event frame.
        await self.send_json({"event": message["event"]})

    # -- helpers --
    @sync_to_async
    def _get_turn(self, raw_id):
        try:
            return Turn.objects.select_related("agent", "agent__workspace", "workspace").get(pk=uuid.UUID(str(raw_id)))
        except (Turn.DoesNotExist, ValueError):
            return None

    @sync_to_async
    def _replay(self, turn, after):
        qs = turn.events.filter(seq__gt=after).order_by("seq")[:500]
        return [groups.serialize_turn_event(e) for e in qs]

    def _after_from_query(self):
        qs = (self.scope.get("query_string") or b"").decode()
        for part in qs.split("&"):
            if part.startswith("after="):
                try:
                    return int(part[6:])
                except ValueError:
                    return 0
        return 0
```
- [ ] **Step 4:** Run `uv run pytest apps/realtime/tests/test_turn_consumer.py -q`. Expected: PASS.
- [ ] **Step 5:** Commit: `feat(realtime): TurnConsumer live-tails the turn ledger`

---

### Task 7: `SupervisorConsumer` — live runner status + waiting counts

**Files:**
- Modify: `apps/realtime/consumers.py` (add SupervisorConsumer)
- Test: `apps/realtime/tests/test_supervisor_consumer.py`

**Interfaces:**
- Consumes: `groups.supervisor_user_group`; a snapshot builder that reuses the same services `/supervisor` REST reads (runners visible to the user + per-agent waiting counts).
- Produces: on connect, one `{"type":"supervisor.snapshot","runners":[...],"waiting":{slug:count}}`; then `supervisor.runner` / `supervisor.waiting` deltas.

- [ ] **Step 1:** Write `apps/realtime/tests/test_supervisor_consumer.py`: anon → 4001; authed → receives a snapshot frame; after a `Runner.save()` (heartbeat) the socket receives a `supervisor.runner` delta.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Add `SupervisorConsumer` to `apps/realtime/consumers.py`:
```python
class SupervisorConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        user = self.scope.get("user")
        if not getattr(user, "is_authenticated", False):
            await self.close(code=4001)
            return
        self.group = groups.supervisor_user_group(user.id)
        await self.channel_layer.group_add(self.group, self.channel_name)
        await self.accept()
        await self.send_json(await self._snapshot(user))

    async def disconnect(self, code):
        grp = getattr(self, "group", None)
        if grp:
            await self.channel_layer.group_discard(grp, self.channel_name)

    async def supervisor_runner(self, message):
        await self.send_json(message)

    async def supervisor_waiting(self, message):
        await self.send_json(message)

    @sync_to_async
    def _snapshot(self, user):
        from apps.realtime.snapshot import supervisor_snapshot
        return supervisor_snapshot(user)
```
- [ ] **Step 4:** Create `apps/realtime/snapshot.py::supervisor_snapshot(user) -> dict` that reuses the existing supervisor read services (the same ones `SupervisorPage` fetches — the fleet needs-you + runner list visible to `user`). Return `{"type": "supervisor.snapshot", "runners": [...], "waiting": {slug: count}}`. Match the field shapes the REST endpoints already return so the frontend view-model is unchanged.
- [ ] **Step 5:** Run `uv run pytest apps/realtime/tests/test_supervisor_consumer.py -q`. Expected: PASS.
- [ ] **Step 6:** Commit: `feat(realtime): SupervisorConsumer snapshot + live deltas`

---

### Task 8: `routing.py` + ASGI websocket wiring

**Files:**
- Create: `apps/realtime/routing.py`
- Modify: `config/asgi.py`
- Test: `apps/realtime/tests/test_routing.py`

**Interfaces:**
- Produces: `websocket_urlpatterns` — `ws/turns/<uuid:turn_id>/` → TurnConsumer, `ws/supervisor/` → SupervisorConsumer. `config.asgi.application` routes `websocket` scope through `RealtimeAuthMiddleware`.

- [ ] **Step 1:** Write `apps/realtime/routing.py`:
```python
from django.urls import path, re_path
from . import consumers

websocket_urlpatterns = [
    re_path(r"^ws/turns/(?P<turn_id>[0-9a-f-]+)/$", consumers.TurnConsumer.as_asgi()),
    path("ws/supervisor/", consumers.SupervisorConsumer.as_asgi()),
]
```
- [ ] **Step 2:** Modify `config/asgi.py`: after `_django_asgi_app = get_asgi_application()` and before the Starlette app, build a Channels `ProtocolTypeRouter`:
```python
from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402
from channels.security.websocket import AllowedHostsOriginValidator  # noqa: E402
from apps.realtime.channels_auth import RealtimeAuthMiddleware  # noqa: E402
from apps.realtime.routing import websocket_urlpatterns  # noqa: E402

_django_with_ws = ProtocolTypeRouter({
    "http": _django_asgi_app,
    "websocket": AllowedHostsOriginValidator(
        RealtimeAuthMiddleware(URLRouter(websocket_urlpatterns))
    ),
})
```
Then change the catch-all mount from `Mount("/", app=_django_asgi_app)` to `Mount("/", app=_django_with_ws)`.
- [ ] **Step 3:** Write `apps/realtime/tests/test_routing.py`: assert `websocket_urlpatterns` resolves a turn UUID path to `TurnConsumer` and `ws/supervisor/` to `SupervisorConsumer` (inspect the patterns), and that importing `config.asgi` builds `application` without error.
- [ ] **Step 4:** Run `uv run pytest apps/realtime/tests/test_routing.py -q`. Expected: PASS. Then `uv run pytest -q` (full backend suite) to confirm the ASGI change didn't break MCP/lifespan wiring.
- [ ] **Step 5:** Commit: `feat(realtime): websocket routing + ASGI ProtocolTypeRouter`

---

### Task 9: Frontend — `wsUrl`, `useLiveTurn`, turn-view integration

**Files:**
- Create: `frontend/src/lib/wsUrl.ts`, `frontend/src/hooks/useLiveTurn.ts`, `frontend/src/api/types.ws.ts`
- Create: `frontend/src/hooks/useLiveTurn.test.ts`
- Modify: the turn detail component that currently polls `GET /turns/{id}/events` (locate via `grep -rn "turns/.*events\|read_turn_events\|harness" frontend/src`)

**Interfaces:**
- Produces: `useLiveTurn(turnId: string): { events: TurnEventFrame[]; connected: boolean; lastError: string | null }`. `wsUrl(path: string): string` respecting `import.meta.env.BASE_URL`.

- [ ] **Step 1:** Write `frontend/src/lib/wsUrl.ts`:
```ts
// Build a ws(s):// URL for a path under the SPA base (honors the /canopy prefix).
export function wsUrl(path: string): string {
  const base = import.meta.env.BASE_URL.replace(/\/$/, "");
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  const clean = path.replace(/^\//, "");
  return `${proto}//${window.location.host}${base}/${clean}`;
}
```
- [ ] **Step 2:** Write `frontend/src/api/types.ws.ts` (`TurnEventFrame = { seq: number; kind: string; payload: unknown; ts: string }`, plus supervisor frame types used in Task 10).
- [ ] **Step 3:** Write `frontend/src/hooks/useLiveTurn.test.ts` (vitest) driving a mock `WebSocket`: assert events merge de-duped by `seq`, reconnect uses the highest seen `seq` in the `?after=` query, and out-of-order/duplicate frames don't double-insert. Factor the merge into a pure `mergeEvents(prev, incoming)` so it unit-tests without a socket.
- [ ] **Step 4:** Run vitest → FAIL.
- [ ] **Step 5:** Write `frontend/src/hooks/useLiveTurn.ts`: a `WebSocket` to `wsUrl(\`ws/turns/${turnId}/?after=${cursor}\`)`, exponential-backoff reconnect (`[1,2,5,10]s`), `mergeEvents` on each `{event}` frame, tracks max seq as the reconnect cursor, exposes `{events, connected, lastError}`.
- [ ] **Step 6:** Run vitest → PASS.
- [ ] **Step 7:** Swap the turn detail component's polling for `useLiveTurn(turnId)`; keep the REST `GET …/events` as the initial/fallback fetch. Run `cd frontend && npm run build`. Expected: type-check + build PASS.
- [ ] **Step 8:** Commit: `feat(realtime): useLiveTurn hook + live turn view`

---

### Task 10: Frontend — `useLiveSupervisor` + SupervisorPage integration

**Files:**
- Create: `frontend/src/hooks/useLiveSupervisor.ts`, `frontend/src/hooks/useLiveSupervisor.test.ts`
- Modify: `frontend/src/pages/SupervisorPage.tsx` (+ `components/supervisor/*` as needed)

**Interfaces:**
- Produces: `useLiveSupervisor(): { runners; waiting; connected }` — applies a `supervisor.snapshot` then `supervisor.runner` / `supervisor.waiting` deltas into a view-model matching what `SupervisorPage` renders today.

- [ ] **Step 1:** Write `useLiveSupervisor.test.ts` — a pure `applyFrame(state, frame)` reducer: snapshot seeds runners+waiting; a `supervisor.runner` frame upserts by runner id; a `supervisor.waiting` frame updates `waiting[slug]`. Unit-test all three.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Write `useLiveSupervisor.ts` (WS to `wsUrl("ws/supervisor/")`, backoff reconnect, `applyFrame`).
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Wire `SupervisorPage` to prefer the live view-model, falling back to its current mount-fetch when the socket is not connected. `cd frontend && npm run build` → PASS.
- [ ] **Step 6:** Commit: `feat(realtime): useLiveSupervisor hook + live /supervisor`

---

## Self-Review

**Spec coverage:** SP1 spec §2 app layout → Task 1; §3 transport → Tasks 1,8; §4 groups/routing → Tasks 2,8; §5 fanout (3 paths, bulk_create caveat) → Tasks 3,4; §6 WS auth → Task 5; §7 replay/snapshot → Tasks 6,7; §8 frontend hooks → Tasks 9,10; §9 boundary → Task 1; §10 testing → every task's TDD steps. All covered.

**Placeholder scan:** The two soft spots are deliberately marked "verify the exact accessor/resolver": `workspace_member_ids` related_name (Task 4 Step 4), the token resolver import (Task 5 Step 3), and the snapshot service reuse (Task 7 Step 4). These are lookups against existing code to confirm during execution, not unspecified logic — each has a concrete fallback described.

**Type consistency:** `turn_group`/`supervisor_user_group`/`serialize_turn_event`/`publish` signatures are identical across Tasks 2, 4, 6, 7. Frame `type` strings (`turn.event`, `supervisor.runner`, `supervisor.waiting`, `supervisor.snapshot`) match between the fanout receivers (Task 4/7) and the consumer group-handler method names (Channels maps `type="turn.event"` → `turn_event`). Consumer close codes (4001/4003/4004) consistent.
