# Chat Kit 1a — Backend: SessionConsumer Canonical-Protocol Upgrade — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade canopy-web's `apps/chat` WebSocket `SessionConsumer` to speak ace-web's canonical chat protocol (enriched snapshot, canonical frame names, `TurnEvent`→`chat.*` stream translation, `chat.stop`), so the ported ace-web frontend kit (plan 1b) connects with minimal adaptation.

**Architecture:** Keep canopy's ledger-as-source-of-truth model. Add pure serializers that shape DB rows into ace's `SessionState`/`Message`/`Draft`/`Participant` DTOs, and a pure `stream_map` that translates a `TurnEvent` dict into canonical `chat.*` frames. The consumer's snapshot, receive-actions, draft broadcasts, and turn-event group handler are rewired to use them. No new models, no migrations — every canonical field is synthesized from existing columns.

**Tech Stack:** Django 5 ASGI, Django Channels (`AsyncJsonWebsocketConsumer`), Django Ninja, pytest + `pytest-asyncio` (`asyncio_mode=auto`), `channels.testing.WebsocketCommunicator`, sqlite `:memory:` for tests.

## Global Constraints

- Framework-tier only: `apps/chat` and `apps/realtime` must not import any product app (enforced by `tests/test_architecture_boundary.py`). Serializers/stream_map import only stdlib + `apps.chat`/`apps.harness` models.
- No new models / no migrations in this plan — synthesize canonical fields from existing columns.
- The `TurnEvent` ledger stays the source of truth; stream translation is a per-connection presentation mapping in the consumer, never a second stream engine. `apps/realtime` fan-out stays generic (keeps publishing `chat.turn_event`).
- Canonical protocol = ace-web `apps/sessions/consumers.py` + `serializers.py`. Reference it for exact field names when in doubt: `/Users/jjackson/emdash-projects/ace-web/apps/sessions/`.
- `message_id` on the wire is a **string** (canopy sends `str(pk)` or a synthetic `"{turn8}:{seq}"`); the kit (plan 1b) treats it as opaque `string`.
- Tests live in the flat top-level `tests/` dir (e.g. `tests/test_chat_serializers.py`), not `apps/chat/tests/`. Match `tests/test_chat_session_consumer.py` style (inline ORM object creation, `@pytest.mark.django_db(transaction=True)` for consumer/on_commit tests, reusable `user`/`workspace`/`agent` fixtures from `conftest.py`).
- Run the whole file's tests after each task; never leave the chat suite red.

---

### Task 1: Canonical DTO serializers

**Files:**
- Create: `apps/chat/serializers.py`
- Test: `tests/test_chat_serializers.py`

**Interfaces:**
- Produces:
  - `message_dto(msg: Message) -> dict` → keys `{id:str, turn_index:int, role:str, content:dict, plaintext:str, status:str, error_detail:str|None, started_at:str|None, completed_at:str|None, created_at:str}`. `status` is `"complete"` for every persisted row (historical/materialized); `error_detail/started_at/completed_at` are `None`; `id = str(msg.pk)`; `created_at = msg.created_at.isoformat()`.
  - `draft_dto(draft: Draft | None) -> dict | None` → `{id:str, slot:str, status:"open", body:str, version:int, last_editor:int|None, last_edit_at:str|None}`; `last_edit_at = draft.updated_at.isoformat()`; `last_editor = draft.last_editor_id`.
  - `participant_dto(sp: SessionParticipant) -> dict` → `{user_id:int, email:str, display_name:str, role:str, joined_at:str|None, last_seen_at:str|None}`. `email = sp.user.email`; `display_name = sp.user.get_full_name() or sp.user.email`; timestamps via `getattr(sp, "created_at"/"last_seen_at", None)` (`.isoformat()` if present else `None`).
  - `session_state_dto(*, session, current_user_id, participants, present_ids, draft, messages) -> dict` → `{messages:[message_dto...], active_draft:draft_dto, participants:[participant_dto...], presence_user_ids:[int...], current_user_id:int}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_chat_serializers.py
import pytest
from apps.chat import serializers
from apps.chat.models import Session, Message, Draft, SessionParticipant

@pytest.mark.django_db
def test_message_dto_shape(user, workspace):
    s = Session.objects.create(workspace=workspace, created_by=user, title="t")
    m = Message.objects.create(session=s, turn_index=0, role="user",
                               content={"text": "hi"}, plaintext="hi")
    dto = serializers.message_dto(m)
    assert dto["id"] == str(m.pk)
    assert dto["turn_index"] == 0
    assert dto["role"] == "user"
    assert dto["plaintext"] == "hi"
    assert dto["status"] == "complete"
    assert dto["error_detail"] is None
    assert set(dto) == {"id", "turn_index", "role", "content", "plaintext",
                        "status", "error_detail", "started_at", "completed_at", "created_at"}

@pytest.mark.django_db
def test_draft_dto_shape_and_none(user, workspace):
    assert serializers.draft_dto(None) is None
    s = Session.objects.create(workspace=workspace, created_by=user, title="t")
    d = Draft.objects.create(session=s, slot="next", body="wip", version=3, last_editor=user)
    dto = serializers.draft_dto(d)
    assert dto["id"] == str(d.pk)
    assert dto["slot"] == "next"
    assert dto["status"] == "open"
    assert dto["body"] == "wip"
    assert dto["version"] == 3
    assert dto["last_editor"] == user.pk
    assert dto["last_edit_at"] is not None

@pytest.mark.django_db
def test_session_state_dto_keys(user, workspace):
    s = Session.objects.create(workspace=workspace, created_by=user, title="t")
    sp = SessionParticipant.objects.create(session=s, user=user, role="owner")
    state = serializers.session_state_dto(
        session=s, current_user_id=user.pk, participants=[sp],
        present_ids=[user.pk], draft=None, messages=[])
    assert set(state) == {"messages", "active_draft", "participants",
                          "presence_user_ids", "current_user_id"}
    assert state["current_user_id"] == user.pk
    assert state["presence_user_ids"] == [user.pk]
    assert state["participants"][0]["email"] == user.email
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_chat_serializers.py -v`
Expected: FAIL — `ModuleNotFoundError: apps.chat.serializers`.

- [ ] **Step 3: Write minimal implementation**

Create `apps/chat/serializers.py` with the four functions exactly per the Interfaces block above. Use `getattr` fallbacks for the optional `SessionParticipant` timestamps. No Django REST framework — plain dict builders.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_chat_serializers.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add apps/chat/serializers.py tests/test_chat_serializers.py
git commit -m "feat(chat): canonical DTO serializers (message/draft/participant/session_state)"
```

---

### Task 2: `stream_map` — TurnEvent → canonical chat.* frames (pure)

**Files:**
- Create: `apps/chat/stream_map.py`
- Test: `tests/test_chat_stream_map.py`

**Interfaces:**
- Consumes: a serialized turn-event dict `{seq:int, kind:str, payload:dict, ts:str}` (the shape `apps/realtime/groups.py::serialize_turn_event` already produces) and a `resolve_message_id(seq:int) -> str` callback.
- Produces: `turn_event_to_frames(evt: dict, resolve_message_id) -> list[dict]`, each frame `{"event": <name>, "data": {...}}`:
  - `kind == "assistant"` → `[{event:"chat.stream_start", data:{message_id, turn_index:evt["seq"]}}, {event:"chat.stream_complete", data:{message_id, plaintext: payload.get("text","")}}]` (whole-message today; a future delta-emitting runner instead yields `chat.delta` — out of scope here).
  - `kind == "tool_start"` → `[{event:"chat.tool_use", data:{parent_message_id:None, tool_message_id:message_id, block: payload}}]`.
  - `kind in ("tool_end","tool_result")` → `[{event:"chat.tool_result", data:{parent_message_id:None, tool_message_id:message_id, block: payload}}]`.
  - `kind == "error"` → `[{event:"chat.stream_error", data:{message_id, detail: payload.get("detail") or payload.get("text","error")}}]`.
  - `kind in ("status","heartbeat","question","approval")` → `[]` (no client frame).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_chat_stream_map.py
from apps.chat.stream_map import turn_event_to_frames

def _rid(seq): return f"m:{seq}"

def test_assistant_maps_to_start_and_complete():
    frames = turn_event_to_frames(
        {"seq": 5, "kind": "assistant", "payload": {"text": "hello"}, "ts": "t"}, _rid)
    assert [f["event"] for f in frames] == ["chat.stream_start", "chat.stream_complete"]
    assert frames[0]["data"]["message_id"] == "m:5"
    assert frames[1]["data"]["plaintext"] == "hello"

def test_tool_events_map():
    assert turn_event_to_frames(
        {"seq": 1, "kind": "tool_start", "payload": {"name": "Bash"}, "ts": "t"}, _rid
    )[0]["event"] == "chat.tool_use"
    assert turn_event_to_frames(
        {"seq": 2, "kind": "tool_end", "payload": {"ok": True}, "ts": "t"}, _rid
    )[0]["event"] == "chat.tool_result"

def test_status_and_heartbeat_are_silent():
    assert turn_event_to_frames({"seq": 1, "kind": "status", "payload": {"status": "running"}, "ts": "t"}, _rid) == []
    assert turn_event_to_frames({"seq": 1, "kind": "heartbeat", "payload": {}, "ts": "t"}, _rid) == []

def test_error_maps_to_stream_error():
    frames = turn_event_to_frames({"seq": 9, "kind": "error", "payload": {"detail": "boom"}, "ts": "t"}, _rid)
    assert frames[0]["event"] == "chat.stream_error"
    assert frames[0]["data"]["detail"] == "boom"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_chat_stream_map.py -v`
Expected: FAIL — `ModuleNotFoundError: apps.chat.stream_map`.

- [ ] **Step 3: Write minimal implementation**

Create `apps/chat/stream_map.py::turn_event_to_frames` per the Interfaces block. Pure function, no Django imports.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_chat_stream_map.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add apps/chat/stream_map.py tests/test_chat_stream_map.py
git commit -m "feat(chat): pure TurnEvent->canonical chat.* frame translation (stream_map)"
```

---

### Task 3: Consumer snapshot → canonical `session.state`

**Files:**
- Modify: `apps/chat/consumers.py` (the `_snapshot`/`session.state` builder, ~L158-177)
- Test: `tests/test_chat_session_consumer.py` (add a snapshot-shape test)

**Interfaces:**
- Consumes: `serializers.session_state_dto` (Task 1).
- Produces: the connect frame is `{"event":"session.state","data": session_state_dto(...)}` — replaces canopy's lean `{participants:[{user_id,role}], present, draft, messages}` with the canonical keys (`messages`, `active_draft`, `participants`, `presence_user_ids`, `current_user_id`).

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_chat_session_consumer.py
@pytest.mark.django_db(transaction=True)
async def test_snapshot_is_canonical(chat_communicator_factory, user, workspace):
    comm, session = await chat_communicator_factory(user, workspace)
    connected, _ = await comm.connect()
    assert connected
    frame = await comm.receive_json_from()
    assert frame["event"] == "session.state"
    data = frame["data"]
    assert set(data) >= {"messages", "active_draft", "participants",
                         "presence_user_ids", "current_user_id"}
    assert data["current_user_id"] == user.pk
    await comm.disconnect()
```

(If a `chat_communicator_factory` fixture does not already exist in the test file, add a small helper that builds a `Session`, ensures the participant, and returns `(WebsocketCommunicator(application, f"/ws/chat/{session.id}/", ...with scope user...), session)` — mirror the existing connect test in the same file.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_chat_session_consumer.py::test_snapshot_is_canonical -v`
Expected: FAIL — snapshot has canopy's old keys (`present`/`draft`), missing `current_user_id`.

- [ ] **Step 3: Write minimal implementation**

In `apps/chat/consumers.py`, replace the `_snapshot` body to gather participants (`SessionParticipant.objects.filter(session=...)`), present ids (`presence.present_ids(...)`), the active draft (`drafts.active_draft(...)`), and recent messages (last 200, ordered by `turn_index`), then return `serializers.session_state_dto(session=..., current_user_id=self.scope["user"].id, participants=..., present_ids=..., draft=..., messages=...)`. Wrap ORM reads in `database_sync_to_async`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_chat_session_consumer.py -v`
Expected: PASS (existing tests + the new one; fix any existing test that asserted the old snapshot keys — update them to canonical).

- [ ] **Step 5: Commit**

```bash
git add apps/chat/consumers.py tests/test_chat_session_consumer.py
git commit -m "feat(chat): emit canonical session.state snapshot (current_user_id, active_draft, full participants)"
```

---

### Task 4: Canonical client→server actions + error envelope

**Files:**
- Modify: `apps/chat/consumers.py` (`receive_json` dispatch + edit-role denial, ~L57-119)
- Test: `tests/test_chat_session_consumer.py`

**Interfaces:**
- Produces: the consumer accepts these actions (renamed/added from canopy's current set):
  - `chat.send` (was `draft.commit`) — commit active draft + execute.
  - `draft.update {version, body}` (was `expected_version`) — accept `version` key.
  - `draft.take_over` (unchanged), `draft.discard` (new — clears active draft, broadcasts `draft.discarded {draft_id}`), `presence.heartbeat` (unchanged), `chat.stop {message_id}` (new — Task 6).
  - Role denial and version/lock failures use the canonical **`session.error`** envelope: `{"event":"session.error","data":{"code":..., "message":..., "detail":...}}` with codes `forbidden`, `draft_version_mismatch` (`detail={current_version,current_body}`), `draft_lock_held` (`detail={holder_user_id,expires_at}`).

- [ ] **Step 1: Write the failing tests**

```python
# add to tests/test_chat_session_consumer.py
@pytest.mark.django_db(transaction=True)
async def test_draft_update_accepts_version_key(chat_communicator_factory, user, workspace):
    comm, session = await chat_communicator_factory(user, workspace)
    await comm.connect(); await comm.receive_json_from()  # snapshot
    await comm.send_json_to({"action": "draft.update", "data": {"version": 0, "body": "hello"}})
    # next broadcast is a canonical draft.updated carrying the new body
    frame = await comm.receive_json_from()
    assert frame["event"] == "draft.updated"
    assert frame["data"]["body"] == "hello"
    await comm.disconnect()

@pytest.mark.django_db(transaction=True)
async def test_version_conflict_uses_session_error(chat_communicator_factory, user, workspace):
    comm, session = await chat_communicator_factory(user, workspace)
    await comm.connect(); await comm.receive_json_from()
    await comm.send_json_to({"action": "draft.update", "data": {"version": 99, "body": "x"}})
    frame = await comm.receive_json_from()
    assert frame["event"] == "session.error"
    assert frame["data"]["code"] == "draft_version_mismatch"
    assert "current_version" in frame["data"]["detail"]
    await comm.disconnect()

@pytest.mark.django_db(transaction=True)
async def test_viewer_denied_with_session_error(chat_communicator_factory, other_user, workspace):
    # other_user joins as viewer; an edit action returns session.error/forbidden
    comm, session = await chat_communicator_factory(other_user, workspace, role="viewer")
    await comm.connect(); await comm.receive_json_from()
    await comm.send_json_to({"action": "draft.update", "data": {"version": 0, "body": "x"}})
    frame = await comm.receive_json_from()
    assert frame["event"] == "session.error"
    assert frame["data"]["code"] == "forbidden"
    await comm.disconnect()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_chat_session_consumer.py -k "version or viewer_denied" -v`
Expected: FAIL — canopy currently uses `draft.commit`/`expected_version`/`draft.conflict`/`error`.

- [ ] **Step 3: Write minimal implementation**

In `receive_json`: rename the `draft.commit` handler to `chat.send`; read `data["version"]` (map to `update_draft(expected_version=...)`); add a `draft.discard` handler; convert the `forbidden`/`draft.conflict`/`draft.locked` outputs to `_error(code, message, detail)` helper emitting `{"event":"session.error","data":{...}}`. Keep `presence.heartbeat`/`draft.take_over`. Reference ace `consumers.py` codes for parity.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_chat_session_consumer.py -v`
Expected: PASS (update any pre-existing test asserting the old `error`/`draft.conflict` names to canonical).

- [ ] **Step 5: Commit**

```bash
git add apps/chat/consumers.py tests/test_chat_session_consumer.py
git commit -m "feat(chat): canonical client actions (chat.send/draft.update{version}/discard) + session.error envelope"
```

---

### Task 5: Canonical draft broadcasts (updated/committed/discarded/lock_changed)

**Files:**
- Modify: `apps/chat/consumers.py` (draft broadcast helpers + the `chat.send` path)
- Test: `tests/test_chat_session_consumer.py`

**Interfaces:**
- Produces server→client draft events with canonical names/shapes:
  - `draft.updated` → full `draft_dto` (id/slot/status/body/version/last_editor/last_edit_at).
  - `draft.lock_changed {draft_id, holder_user_id, expires_at}` (on take-over; `expires_at = updated_at + IDLE_WINDOW`).
  - `draft.committed {draft_id, user_message_id}` (on `chat.send`, before the cleared `draft.updated`).
  - `draft.discarded {draft_id}` (on `draft.discard`).

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_chat_session_consumer.py
@pytest.mark.django_db(transaction=True)
async def test_send_broadcasts_draft_committed(chat_communicator_factory, user, workspace):
    comm, session = await chat_communicator_factory(user, workspace)
    await comm.connect(); await comm.receive_json_from()  # snapshot
    await comm.send_json_to({"action": "draft.update", "data": {"version": 0, "body": "ping"}})
    await comm.receive_json_from()  # draft.updated
    await comm.send_json_to({"action": "chat.send", "data": {}})
    events = [ (await comm.receive_json_from())["event"] for _ in range(2) ]
    assert "draft.committed" in events
    await comm.disconnect()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_chat_session_consumer.py::test_send_broadcasts_draft_committed -v`
Expected: FAIL — canopy broadcasts only a cleared `draft.updated`, no `draft.committed`.

- [ ] **Step 3: Write minimal implementation**

In the `chat.send` path: call `send_message(...)`, capture the user `Message`, group-broadcast `draft.committed {draft_id, user_message_id}` then the cleared `draft.updated`, then `maybe_execute_inline(turn)`. Make the draft-update broadcast use `draft_dto`. Add the `lock_changed` broadcast on take-over. Add `draft.discarded` on discard.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_chat_session_consumer.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/chat/consumers.py tests/test_chat_session_consumer.py
git commit -m "feat(chat): canonical draft broadcasts (committed/discarded/lock_changed, full draft_dto)"
```

---

### Task 6: Turn-event group handler emits canonical stream frames + `chat.stop`

**Files:**
- Modify: `apps/chat/consumers.py` (the `chat_turn_event` group handler ~L122; add `chat.stop` action)
- Test: `tests/test_chat_session_consumer.py`, `tests/test_chat_integration.py`

**Interfaces:**
- Consumes: `stream_map.turn_event_to_frames` (Task 2); `apps/realtime/groups.py::serialize_turn_event`.
- Produces:
  - The `chat_turn_event` group handler resolves a `message_id` for the event (a projected `Message` for that `(turn, source_seq)` if present, else synthetic `f"{turn_id[:8]}:{seq}"`), runs `turn_event_to_frames`, and sends each canonical frame to the client. Replaces canopy's raw `chat.turn_event` passthrough.
  - `chat.stop {message_id}` action → cancels the running turn via harness (`POST`-equivalent service `cancel_turn`) and emits `chat.stream_cancelled {message_id, partial_len}`.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_chat_session_consumer.py
@pytest.mark.django_db(transaction=True)
async def test_assistant_event_streams_canonical_frames(chat_communicator_factory, user, workspace, settings):
    settings.CHAT_STUB_EXECUTOR = True  # stub appends a whole assistant event
    comm, session = await chat_communicator_factory(user, workspace)
    await comm.connect(); await comm.receive_json_from()  # snapshot
    await comm.send_json_to({"action": "draft.update", "data": {"version": 0, "body": "hi"}})
    await comm.receive_json_from()  # draft.updated
    await comm.send_json_to({"action": "chat.send", "data": {}})
    # drain frames until we observe the canonical assistant stream frames
    seen = set()
    for _ in range(12):
        f = await comm.receive_json_from()
        seen.add(f["event"])
        if "chat.stream_complete" in seen:
            break
    assert "chat.stream_start" in seen
    assert "chat.stream_complete" in seen
    await comm.disconnect()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_chat_session_consumer.py::test_assistant_event_streams_canonical_frames -v`
Expected: FAIL — canopy sends `chat.turn_event`, never `chat.stream_start/complete`.

- [ ] **Step 3: Write minimal implementation**

Rewrite the `chat_turn_event` handler to translate via `stream_map` (resolving `message_id` from the projected `Message` by `turn=... , content__source_seq=seq`, else synthetic). Add the `chat.stop` action calling the harness cancel service and emitting `chat.stream_cancelled`. Confirm `apps/realtime/signals.py` still publishes the generic `chat.turn_event` group message (unchanged) — only the consumer's handling changes.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_chat_session_consumer.py tests/test_chat_integration.py -v`
Expected: PASS (fix `test_chat_integration.py` if it asserted the old `chat.turn_event` frame — update to canonical).

- [ ] **Step 5: Commit**

```bash
git add apps/chat/consumers.py tests/test_chat_session_consumer.py tests/test_chat_integration.py
git commit -m "feat(chat): consumer translates ledger events to canonical chat.stream_* frames + chat.stop"
```

---

### Task 7: Full-stack canonical round-trip + regression sweep

**Files:**
- Modify: `tests/test_chat_e2e.py` (extend the authenticated round-trip)
- Test: same

**Interfaces:**
- Consumes: everything above, through the assembled ASGI stack.
- Produces: proof that connect→`session.state`(canonical)→`draft.update`→`chat.send`→`draft.committed`→`chat.stream_start`→`chat.stream_complete` all flow through `uvicorn`/`ProtocolTypeRouter`/`RealtimeAuthMiddleware`.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_chat_e2e.py
@pytest.mark.django_db(transaction=True)
async def test_canonical_round_trip(chat_communicator_factory, user, workspace, settings):
    settings.CHAT_STUB_EXECUTOR = True
    comm, session = await chat_communicator_factory(user, workspace)
    ok, _ = await comm.connect(); assert ok
    snap = await comm.receive_json_from()
    assert snap["event"] == "session.state" and snap["data"]["current_user_id"] == user.pk
    await comm.send_json_to({"action": "draft.update", "data": {"version": 0, "body": "hi there"}})
    assert (await comm.receive_json_from())["event"] == "draft.updated"
    await comm.send_json_to({"action": "chat.send", "data": {}})
    events = []
    for _ in range(14):
        events.append((await comm.receive_json_from())["event"])
        if "chat.stream_complete" in events: break
    assert "draft.committed" in events
    assert "chat.stream_start" in events and "chat.stream_complete" in events
    await comm.disconnect()
```

- [ ] **Step 2: Run test to verify it fails then passes**

Run: `uv run pytest tests/test_chat_e2e.py::test_canonical_round_trip -v`
Expected: PASS if Tasks 3-6 are complete (this is an integration assertion, not new production code). If it fails, the failure pinpoints a gap in the frame wiring — fix in the relevant task's file.

- [ ] **Step 3: Full chat + realtime + boundary regression**

Run: `uv run pytest tests/test_chat_*.py tests/test_realtime_*.py tests/test_architecture_boundary.py -q`
Expected: all pass (no framework→product import introduced; no realtime regression).

- [ ] **Step 4: Commit**

```bash
git add tests/test_chat_e2e.py
git commit -m "test(chat): full-stack canonical protocol round-trip"
```

---

## Self-Review

- **Spec coverage:** snapshot enrichment (T3), frame alignment (T4/T5), `TurnEvent`→`chat.*` translation (T2/T6), `chat.stop` (T6), `session.error` envelope (T4), no-migration constraint (synthesized DTOs, T1) — all mapped. Token deltas are explicitly out of scope (stub → whole-message; noted in T2). The kit/frontend is plan 1b.
- **Placeholder scan:** none — every step has concrete test code or a concrete modify instruction against a named symbol/line.
- **Type consistency:** `message_id` is a string everywhere (T2 resolver, T6 handler, kit note). `session_state_dto` keys match across T1/T3. `draft_dto` used by T3 snapshot and T5 broadcasts identically. Action names (`chat.send`, `draft.update{version}`, `draft.discard`, `chat.stop`) consistent across T4/T5/T6.
- **Open dependency:** Task 6 calls a harness `cancel_turn` service. If only the REST view exists (`POST /turns/{id}/cancel`) and not a plain service function, add a thin `apps/harness/services.py::cancel_turn(turn)` in T6 Step 3 and call it from both the view and the consumer (DRY). Verify during T6.
