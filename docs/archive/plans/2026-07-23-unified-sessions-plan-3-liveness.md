# Unified Runner Sessions — Plan 3: Liveness, Runner Streaming & SessionLink Fold Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a runner session *fully live while viewing* — fold the durable `SessionLink` reuse mapping into `RunnerBinding` (one source of truth), stream a local runner's transcript up to attached viewers on demand (attach/detach), and backfill a local session's full history off the runner into `Message` rows when the client asks — all behind unit-testable seams so every task ends green in this repo.

**Architecture:** `RunnerBinding` (the one-to-one on `canopy_sessions.Session`, added in Plan 1) becomes the single durable+live reuse record: it absorbs `SessionLink`'s `thread_key` / `host` / `agent_task_ext_id` / `summary` and gains a partial unique on `(runner, session_key)`; `SessionLink` is deleted. Liveness is a `stream_desired` flag on the binding, toggled by a cache-counted attach registry that the chat WS (and a REST pair) drive; a transition publishes a control frame to the bound runner's `runner.{id}` group AND is observable by a poll endpoint. The runner tails the desired sessions' transcripts (reusing `TailReader` + `chat_bridge`) and posts assistant events to a server endpoint that fans them to the session group as the **same** `chat.stream_*` frames the chat path already uses. Backfill is a second flag (`backfill_requested`): the client asks, the server signals the bound runner, the runner ships its transcript, and the server writes `Message` rows **once** (server-full thereafter). Every runner-facing behavior is exercised with a fake transcript file + a fake harness client + an injected clock — no live laptop.

**Tech Stack:** Django 5 ASGI, Django-Ninja + Pydantic v2, Django Channels, PostgreSQL, pytest (backend); stdlib-only `canopy_runner` package with plain pytest (runner).

## Global Constraints

- **No backwards compatibility, no data preservation.** Single user; `SessionLink` rows, `chat_*`/`canopy_sessions_*` rows may be wiped. Migrations may drop data.
- **Framework boundary holds.** `apps/canopy_sessions`, `apps/harness`, `apps/realtime` are all framework apps (`tests/test_architecture_boundary.py::FRAMEWORK`); no framework→product imports. `harness` and `realtime` importing `canopy_sessions` models is allowed (both framework) and already done (`apps/harness/services.py:660`). The boundary test must stay green.
- **The WS protocol strings are frozen.** `session.state` and the `chat.stream_*` / `chat.tool_use` / `chat.tool_result` / `chat.stream_error` frame names are the canonical ace-web protocol — `apps/canopy_sessions/stream_map.py` owns the mapping and is NOT edited. Live runner events reuse it verbatim.
- **The runner-facing wire shapes carried over from Plan 1 stay frozen where the fold touches them.** `ResolveSessionOut` (`reuse, new_thread, emdash_task_id, agent_task_ext_id, summary, link_id`), `RecordSessionIn`, `ReportedSessionIn`, `EmdashSessionOut` keep their exact fields (`apps/harness/schemas.py:83-138`). The fold changes *storage*, not these shapes — so Tasks 1 & 2 need **no** `gen:api`.
- **Every NEW schema/route regenerates types.** Tasks that add a Ninja route or Schema (Tasks 3, 4, 6) change `frontend/src/api/generated.ts` and must regenerate + commit it. **The `regen-openapi` CI job fails the PR if `generated.ts` is stale** — Plans 1 and 2 both hit this gate. Recipe (backend up on :8000): `cd frontend && npm run gen:api`. Offline fallback: dump the schema, then `npm run gen:api:local`:
  ```bash
  uv run python -c "import os,django,json; os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings.test'); django.setup(); from apps.api.api import api; json.dump(api.get_openapi_schema(), open('frontend/openapi.json','w'))"
  cd frontend && npm run gen:api:local
  ```
- Backend tests: `uv run pytest` (one: `uv run pytest tests/path::name -v`). Runner tests: `cd packages/canopy_runner && uv run pytest` (plain pytest, no DB, no Django — see `packages/canopy_runner/pyproject.toml [tool.pytest.ini_options]`; the root suite ignores `packages/`).

## Deferred to Plan 4 (do NOT build here)

State it explicitly so a task never grows a UI leg:

- **The whole frontend surface** — one unified Sessions list, `ChatPanel` attach-on-open (calling `POST /api/chat/{id}/attach` on mount and `.../detach` on unmount), the running/idle indicator, the "Load earlier" / "Load full" buttons wiring Plan 2's cursor + this plan's backfill, the runner-offline / history-unavailable banner, and the `/api/chat`→`/api/sessions` + `ws/chat/`→`ws/sessions/` URL rename. **Plan 3 delivers the backend + runner capability + the API contracts Plan 4 consumes.** `ChatPage.tsx` keeps rendering the tail unchanged; no page needs edits this plan.
- **Cloud-runner full-transcript persistence** already exists (the `turn_events_appended` → `project_events` projection writes `Message` rows as a cloud chat turn runs). Plan 3 only adds the *local*-runner on-demand backfill; it does not touch the cloud projection.

## Context (verified against the tree at plan time)

- `RunnerBinding` lives in `apps/canopy_sessions/models.py:162-187` (fields: `session` 1:1, `runner` FK SET_NULL, `session_key`, `tail`, `summary`, `status`, `last_interacted_at`, `live_seen_at`, `updated_at`). It has **no** `thread_key` / `host` / `agent_task_ext_id` / `stream_desired` / `backfill_requested` yet, and **no** unique constraint.
- `SessionLink` lives in `apps/harness/models.py:335-453` with `reusable_by(runner)` at `:444`. Its durable half is `(agent XOR project+workspace, thread_key)` + `summary` + `agent_task_ext_id`; its live half is `live_runner` / `live_host` / `live_emdash_task_id` / `live_session_id` / `live_seen_at`.
- `resolve_session` (`apps/harness/services.py:571`), `record_session` (`:614`), `_link_target` (`:546`), and the `replace_reported_sessions` SessionLink loop (`:702-711`) are the fold's blast radius. `_aware` (`:602`) and `timezone` are already imported in `services.py`.
- Runner routes `resolve-session` (`apps/harness/api.py:311`), `record-session` (`:326`), `report_sessions` (`:350`) — all call the services above with the runner already resolved via `_runner_or_404` (`:114`). `_agent_or_404` and `_project_workspace_or_404` (`:290`) gate tenancy.
- SessionLink is referenced by tests: `tests/test_harness_session_link.py`, `tests/test_harness_project_session_api.py`, `tests/test_harness_project_turns.py`, `tests/test_harness_api.py`, `tests/test_harness_runner_capabilities.py`, `tests/test_harness_emdash_sessions.py`, `tests/test_mobile_loop_e2e.py`. These get rewritten/pruned in Task 2.
- The chat WS: `SessionConsumer.connect`/`disconnect` (`apps/canopy_sessions/consumers.py:27,52`) already call `presence.touch`/`presence.leave`. `chat_turn_event` (`:190`) maps a `{event, turn_id}` group frame through `stream_map.turn_event_to_frames` (`:194`) — with `turn_id` falsy it derives `f"seq:{seq}"` message ids (`_resolve_message_id_sync`, `:178`), so a **turn-less** live frame maps cleanly.
- `apps/realtime/groups.py`: `runner_group(id)` (`:35`), `session_group(id)` (`:54`), `publish(group, message)` (`:84`, null-safe). `RunnerConsumer` (`apps/realtime/consumers.py:146`) dispatches group frames by `type` with dots→underscores (`runner.interject` → `runner_interject`, `:189`); it already forwards per-runner control frames to the socket.
- `apps/canopy_sessions/services.py`: Plan-2 helpers `tail_messages`/`messages_before`/`all_messages` (`:38-77`); `project_events` (`:189`) and `_next_index` (`:95`) are the Message-writing precedent; `_ROLE_FOR_KIND` (`:21`) maps event kind→`Message.role`.
- Runner package: `chat_bridge.read_records` / `new_assistant_texts` / `bridge_response` (`packages/canopy_runner/canopy_runner/chat_bridge.py`), `TailReader` (`tail.py`), the change-driven `_maybe_report_sessions` + `_tail_readers` (`main.py:186-249`), `run_once` (`main.py:252`), `Client` (`client.py`), `Config` (`config.py`). Runner test patterns: fake client class + `tmp_path` transcript + `monkeypatch` (see `tests/test_session_report_live.py`, `tests/test_execute_chat.py`, `tests/test_chat_bridge.py`).
- Migration heads: `apps/harness/migrations/0021_runnerbinding.py`; `apps/canopy_sessions/migrations/0005_runnerbinding.py`. Verify with `ls` before creating files.

---

### Task 1: `RunnerBinding` absorbs `SessionLink`'s reuse fields; drop `SessionLink`

Make the binding the one durable+live reuse record. It gains the three fields `SessionLink` had that the binding lacked (`thread_key`, `host`, `agent_task_ext_id`), a ported `reusable_by`, and the partial unique on `(runner, session_key)` that Plan 1's review deferred. `SessionLink` (model + its `0002` fields) is deleted. This task is model + migrations only; the services rewrite that *uses* the fields is Task 2, so the suite stays green here by leaving `services.py` untouched (it still writes `SessionLink`, which still exists until Task 2 — so delete `SessionLink` LAST, in Task 2, not here).

> **Ordering note:** to keep every task green, Task 1 only *adds* fields to `RunnerBinding` (nothing reads them yet) and does **not** delete `SessionLink`. Task 2 rewrites the services onto the new fields and *then* deletes `SessionLink` in the same commit. So Task 1 has no dependency on `SessionLink` being gone.

**Files:**
- Modify: `apps/canopy_sessions/models.py` (the `RunnerBinding` class, `:162-187`)
- Create: `apps/canopy_sessions/migrations/0006_runnerbinding_reuse_fields.py`
- Test: `tests/test_runner_binding.py` (append)

**Interfaces:**
- Produces: `RunnerBinding.thread_key` (CharField 255, blank default ""), `RunnerBinding.host` (CharField 200, blank default ""), `RunnerBinding.agent_task_ext_id` (CharField 255, blank default ""), and `RunnerBinding.reusable_by(runner: Runner) -> bool`.
- Produces: a partial `UniqueConstraint(fields=["runner","session_key"], condition=Q(runner__isnull=False) & ~Q(session_key=""), name="one_binding_per_runner_session_key")`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_runner_binding.py  (append)
def test_binding_reusable_by_matches_runner_and_host(db):
    from apps.canopy_sessions.models import RunnerBinding, Session
    from apps.harness.models import Runner
    from apps.workspaces.models import Workspace

    ws = Workspace.objects.create(slug="w1", name="W1")
    runner = Runner.objects.create(name="laptop", workspace=ws, host="jj@air", location=Runner.LOCAL)
    session = Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER, title="t")
    b = RunnerBinding.objects.create(
        session=session, runner=runner, host="jj@air", session_key="feat-x",
        thread_key="phone:jj:canopy-web", agent_task_ext_id="TASK-9",
    )
    assert b.reusable_by(runner) is True
    # different host on the same runner id -> not reusable (two-account failover invariant)
    runner.host = "jj@studio"
    assert b.reusable_by(runner) is False


def test_binding_partial_unique_on_runner_session_key(db):
    from django.db import IntegrityError, transaction
    from apps.canopy_sessions.models import RunnerBinding, Session
    from apps.harness.models import Runner
    from apps.workspaces.models import Workspace

    ws = Workspace.objects.create(slug="w1", name="W1")
    runner = Runner.objects.create(name="laptop", workspace=ws, location=Runner.LOCAL)
    s1 = Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER, title="a")
    s2 = Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER, title="b")
    RunnerBinding.objects.create(session=s1, runner=runner, session_key="feat-x")
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            RunnerBinding.objects.create(session=s2, runner=runner, session_key="feat-x")


def test_binding_empty_session_key_not_deduped(db):
    # session_key="" is the transient pre-create state; the partial constraint
    # excludes it so two half-formed bindings on one runner don't collide.
    from apps.canopy_sessions.models import RunnerBinding, Session
    from apps.harness.models import Runner
    from apps.workspaces.models import Workspace

    ws = Workspace.objects.create(slug="w1", name="W1")
    runner = Runner.objects.create(name="laptop", workspace=ws, location=Runner.LOCAL)
    s1 = Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER, title="a")
    s2 = Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER, title="b")
    RunnerBinding.objects.create(session=s1, runner=runner, session_key="")
    RunnerBinding.objects.create(session=s2, runner=runner, session_key="")  # no IntegrityError
    assert RunnerBinding.objects.filter(session_key="").count() == 2
```

Add `import pytest` at the top of `tests/test_runner_binding.py` if not present.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_runner_binding.py -k "reusable_by or partial_unique or empty_session_key" -v`
Expected: FAIL — `TypeError: 'host' is an invalid keyword argument` / `AttributeError: 'RunnerBinding' object has no attribute 'reusable_by'`.

- [ ] **Step 3: Add the fields + method + constraint to `RunnerBinding`**

In `apps/canopy_sessions/models.py`, extend the `RunnerBinding` class. Add the three fields after `session_key`:

```python
    # Engine-agnostic handle the runner uses to resume/inject (was emdash_task).
    session_key = models.CharField(max_length=255, blank=True, default="")
    # Durable thread identity (absorbed from SessionLink). For a chat session this
    # is str(session.id); for a phone/agent/project thread it's the topic key
    # (e.g. "phone:jj:canopy-web" or "<target>:<turn_id>"). The reuse lookup keys on
    # (session's target, thread_key).
    thread_key = models.CharField(max_length=255, blank=True, default="", db_index=True)
    # The macOS host that owns the live session — emdash is per-macOS-account, so a
    # session is reusable ONLY by the runner whose host matches (two-account failover).
    host = models.CharField(max_length=200, blank=True, default="")
    # Durable board-task context carried for rehydration (was SessionLink.agent_task_ext_id).
    agent_task_ext_id = models.CharField(max_length=255, blank=True, default="")
```

(Change `session_key`'s existing declaration to add `blank=True, default=""` as shown — the create path sets it after the emdash task exists.)

Add the method after `__str__`:

```python
    def reusable_by(self, runner) -> bool:
        """True if this runner owns the live session (same runner + same macOS host)
        and a concrete session_key is recorded. The runner STILL verifies the task
        exists in its own emdash before driving it — this is the server-side gate.
        Ported verbatim from the retired SessionLink.reusable_by."""
        return bool(
            self.session_key
            and self.runner_id == runner.id
            and self.host
            and self.host == runner.host
        )
```

Add the constraint to `Meta`:

```python
    class Meta:
        ordering = ["-last_interacted_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["runner", "session_key"],
                condition=models.Q(runner__isnull=False) & ~models.Q(session_key=""),
                name="one_binding_per_runner_session_key",
            ),
        ]
```

- [ ] **Step 4: Generate the migration**

Run: `ls apps/canopy_sessions/migrations/` (confirm head is `0005_runnerbinding`), then
`uv run python manage.py makemigrations canopy_sessions --name runnerbinding_reuse_fields`
Expected: `apps/canopy_sessions/migrations/0006_runnerbinding_reuse_fields.py` — `AddField` ×3, `AlterField` session_key, `AddConstraint`.

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_runner_binding.py -v && uv run python manage.py makemigrations --check --dry-run`
Expected: PASS; "No changes detected".

- [ ] **Step 6: Commit**

```bash
git add apps/canopy_sessions/models.py apps/canopy_sessions/migrations/0006_runnerbinding_reuse_fields.py tests/test_runner_binding.py
git commit -m "feat(sessions): RunnerBinding absorbs SessionLink reuse fields + partial unique (runner, session_key)"
```

---

### Task 2: Rewrite `resolve_session`/`record_session`/`replace_reported_sessions` onto `RunnerBinding`; delete `SessionLink`

The binding is now the reuse authority. Rewrite the three service functions to read/write `RunnerBinding` keyed by `(session's target, thread_key)`, keep the `ResolveSessionOut` wire shape frozen (runner routes unchanged — they call the same signatures), drop the redundant SessionLink upsert from the report path, and delete the `SessionLink` model. Data is disposable, so the delete migration needs no data move.

**Files:**
- Modify: `apps/harness/services.py` (`resolve_session` `:571`, `record_session` `:614`, `replace_reported_sessions` `:702-711`, `_link_target` `:546` — delete it)
- Modify: `apps/harness/models.py` (delete `class SessionLink` `:335-453`)
- Modify: `apps/harness/api.py` (remove any `SessionLink` import; routes are otherwise unchanged) and `apps/harness/schemas.py` (remove any `SessionLink` import — the `ResolveSessionOut`/`RecordSessionIn` schemas themselves stay)
- Create: `apps/harness/migrations/0022_delete_sessionlink.py`
- Test: rewrite `tests/test_harness_session_link.py` → `tests/test_harness_session_reuse.py`; fix `tests/test_harness_project_session_api.py`, `tests/test_harness_project_turns.py`, `tests/test_harness_api.py`, `tests/test_harness_runner_capabilities.py`, `tests/test_harness_emdash_sessions.py`, `tests/test_mobile_loop_e2e.py`

**Interfaces:**
- Consumes: `RunnerBinding.thread_key/host/agent_task_ext_id/reusable_by` (Task 1); `Session.ORIGIN_RUNNER`.
- Produces (signatures UNCHANGED — the runner routes and `execute.py` client calls are untouched):
  - `resolve_session(agent, thread_key, runner, *, project="", workspace=None) -> dict` returning `{reuse, emdash_task_id, agent_task_ext_id, summary, link_id, new_thread}` (`link_id` is now `str(binding.session_id)`).
  - `record_session(agent, thread_key, *, runner, project="", workspace=None, emdash_task_id="", session_id="", agent_task_ext_id=None, summary=None) -> RunnerBinding`.
  - `replace_reported_sessions(runner, workspace, sessions) -> int` (same as Plan 1 minus the SessionLink loop).
- Produces (new private helpers): `_binding_for_thread(agent, project, workspace, thread_key)` and `_thread_session(agent, project, workspace, thread_key)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_harness_session_reuse.py  (new — replaces test_harness_session_link.py)
import uuid
import pytest
from apps.harness import services
from apps.harness.models import Runner
from apps.canopy_sessions.models import RunnerBinding, Session
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db


def _agent(ws, slug="echo"):
    from apps.agents.models import Agent
    return Agent.objects.create(slug=slug, name=slug.title(), workspace=ws)


def test_resolve_new_thread_when_no_binding():
    ws = Workspace.objects.create(slug="w1", name="W1")
    a = _agent(ws)
    r = Runner.objects.create(name="laptop", workspace=ws, host="jj@air", location=Runner.LOCAL)
    plan = services.resolve_session(a, "phone:jj:echo", r)
    assert plan["new_thread"] is True
    assert plan["reuse"] is False
    assert plan["link_id"] is None


def test_record_then_resolve_reuses_for_same_runner_host():
    ws = Workspace.objects.create(slug="w1", name="W1")
    a = _agent(ws)
    r = Runner.objects.create(name="laptop", workspace=ws, host="jj@air", location=Runner.LOCAL)
    services.record_session(a, "phone:jj:echo", runner=r,
                            emdash_task_id="echo-1234", summary="rolling ctx",
                            agent_task_ext_id="T-9")
    plan = services.resolve_session(a, "phone:jj:echo", r)
    assert plan["reuse"] is True
    assert plan["emdash_task_id"] == "echo-1234"
    assert plan["summary"] == "rolling ctx"
    assert plan["agent_task_ext_id"] == "T-9"
    # exactly one durable Session was created for the thread
    assert Session.objects.filter(agent=a, origin=Session.ORIGIN_RUNNER).count() == 1


def test_record_is_idempotent_per_thread():
    ws = Workspace.objects.create(slug="w1", name="W1")
    a = _agent(ws)
    r = Runner.objects.create(name="laptop", workspace=ws, host="jj@air", location=Runner.LOCAL)
    services.record_session(a, "phone:jj:echo", runner=r, emdash_task_id="echo-1")
    services.record_session(a, "phone:jj:echo", runner=r, emdash_task_id="echo-2")
    assert Session.objects.filter(agent=a).count() == 1
    b = RunnerBinding.objects.get(thread_key="phone:jj:echo")
    assert b.session_key == "echo-2"  # re-pointed at the newest live task


def test_record_binds_existing_chat_session_by_uuid_thread_key():
    ws = Workspace.objects.create(slug="w1", name="W1")
    a = _agent(ws)
    r = Runner.objects.create(name="laptop", workspace=ws, host="jj@air", location=Runner.LOCAL)
    chat = Session.objects.create(workspace=ws, agent=a, origin=Session.ORIGIN_WEB, title="web chat")
    services.record_session(a, str(chat.id), runner=r, emdash_task_id="echo-9")
    # binds the EXISTING web session, does not fork a new runner session
    assert Session.objects.filter(agent=a).count() == 1
    b = RunnerBinding.objects.get(session=chat)
    assert b.session_key == "echo-9"
    assert b.thread_key == str(chat.id)


def test_reuse_denied_for_different_host():
    ws = Workspace.objects.create(slug="w1", name="W1")
    a = _agent(ws)
    r = Runner.objects.create(name="laptop", workspace=ws, host="jj@air", location=Runner.LOCAL)
    services.record_session(a, "phone:jj:echo", runner=r, emdash_task_id="echo-1")
    r.host = "jj@studio"  # other macOS account claims the same runner id
    plan = services.resolve_session(a, "phone:jj:echo", r)
    assert plan["reuse"] is False
    assert plan["emdash_task_id"] == "echo-1"  # hint still returned for rehydration context


def test_project_reuse_is_workspace_scoped():
    ws = Workspace.objects.create(slug="w1", name="W1")
    ws2 = Workspace.objects.create(slug="w2", name="W2")
    r = Runner.objects.create(name="laptop", workspace=ws, host="jj@air", location=Runner.LOCAL)
    services.record_session(None, "phone:jj:canopy-web", runner=r, project="canopy-web",
                            workspace=ws, emdash_task_id="cw-1")
    # a guessed thread_key from another workspace must NOT hijack the link
    other = services.resolve_session(None, "phone:jj:canopy-web", r, project="canopy-web", workspace=ws2)
    assert other["new_thread"] is True
    same = services.resolve_session(None, "phone:jj:canopy-web", r, project="canopy-web", workspace=ws)
    assert same["reuse"] is True


def test_sessionlink_is_gone():
    import apps.harness.models as m
    assert not hasattr(m, "SessionLink")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_harness_session_reuse.py -v`
Expected: FAIL — `resolve_session`/`record_session` still hit `SessionLink`; `test_sessionlink_is_gone` fails.

- [ ] **Step 3: Rewrite the services**

In `apps/harness/services.py`, delete `_link_target` (`:546-568`) and replace `resolve_session`/`record_session` (`:571-652`) with:

```python
def _binding_for_thread(agent, project, workspace, thread_key):
    """The RunnerBinding for a (target, thread_key), or None. Enforces the
    agent-XOR-project rule the way _link_target used to: an agent thread matches on
    session.agent and ignores workspace (derived via the agent); a project thread
    matches on session.project AND session.workspace (its identity, so a guessed
    thread_key from another tenant lands on its own row, never the victim's)."""
    from apps.canopy_sessions.models import RunnerBinding

    if bool(agent) == bool(project):
        raise ValueError("a session reuse lookup targets an agent XOR a project")
    qs = RunnerBinding.objects.select_related("session", "runner").filter(thread_key=thread_key)
    if agent:
        return qs.filter(session__agent=agent).first()
    if workspace is None:
        raise ValueError("a project session reuse lookup needs a workspace")
    return qs.filter(
        session__agent__isnull=True, session__project=project, session__workspace=workspace
    ).first()


def _thread_session(agent, project, workspace, thread_key):
    """Find-or-create the durable Session a thread maps to. A chat thread_key is
    str(session.id) — bind that exact existing Session. Otherwise create a durable
    origin=runner Session for the phone/agent/project thread."""
    from apps.canopy_sessions.models import Session

    try:
        existing = Session.objects.filter(pk=uuid.UUID(str(thread_key))).first()
    except (ValueError, TypeError):
        existing = None
    if existing is not None:
        return existing
    return Session.objects.create(
        agent=agent,
        project=project or "",
        workspace=workspace or (agent.workspace if agent else None),
        origin=Session.ORIGIN_RUNNER,
        title=thread_key[:200],
    )


def resolve_session(agent, thread_key: str, runner: Runner, *, project: str = "", workspace=None) -> dict:
    """Given (target, thread_key) and the CURRENTLY-active runner, decide how to
    execute — reuse this runner's live session, or spawn fresh + rehydrate. Reuse is
    only proposed when the binding's runner + macOS host match the caller (the
    two-account failover invariant). Wire shape frozen (ResolveSessionOut)."""
    binding = _binding_for_thread(agent, project, workspace, thread_key)
    if binding is None:
        return {"reuse": False, "emdash_task_id": "", "agent_task_ext_id": "",
                "summary": "", "link_id": None, "new_thread": True}
    return {
        "reuse": binding.reusable_by(runner),
        "emdash_task_id": binding.session_key,
        "agent_task_ext_id": binding.agent_task_ext_id,
        "summary": binding.summary,
        "link_id": str(binding.session_id),
        "new_thread": False,
    }


def record_session(
    agent,
    thread_key: str,
    *,
    runner: Runner,
    project: str = "",
    workspace=None,
    emdash_task_id: str = "",
    session_id: str = "",  # accepted for wire-compat; the binding keys on session_key
    agent_task_ext_id: str | None = None,
    summary: str | None = None,
):
    """Upsert the thread's durable Session + RunnerBinding and re-point the live-session
    hint at THIS runner/host. Only overwrites agent_task_ext_id/summary when passed,
    preserving accumulated context. The API caller has already gated the runner's
    pairer against `workspace` — this stores, it does not authorize."""
    from apps.canopy_sessions.models import RunnerBinding

    with transaction.atomic():
        binding = _binding_for_thread(agent, project, workspace, thread_key)
        if binding is None:
            session = _thread_session(agent, project, workspace, thread_key)
            binding = (
                RunnerBinding.objects.select_for_update()
                .filter(session=session)
                .first()
            )
            if binding is None:
                binding = RunnerBinding(session=session)
        binding.thread_key = thread_key
        binding.runner = runner
        binding.host = runner.host
        binding.session_key = emdash_task_id
        binding.live_seen_at = timezone.now()
        if agent_task_ext_id is not None:
            binding.agent_task_ext_id = agent_task_ext_id
        if summary is not None:
            binding.summary = summary
        binding.save()
    return binding
```

Add `import uuid` to `apps/harness/services.py`'s imports if not already present (check the top of the file).

- [ ] **Step 4: Drop the SessionLink loop from `replace_reported_sessions`**

In `apps/harness/services.py`, delete the redundant SessionLink upsert loop (`:702-711`, the `for s in deduped: if s.project: record_session(...)` block and its comment). The reported `RunnerBinding` rows the function already writes ARE the durable record now; a second mapping was the SessionLink-era duplicate. Leave the rest of the function (binding upsert, live-pointer clear, `on_commit` fire) unchanged.

- [ ] **Step 5: Delete the `SessionLink` model + its imports**

- Remove `class SessionLink(models.Model): ...` (`apps/harness/models.py:335-453`).
- `grep -n "SessionLink" apps/harness/*.py` and remove any lingering imports/references in `api.py`, `schemas.py` (the `ResolveSessionOut`/`RecordSessionIn` *schemas* stay; only a `SessionLink` symbol import, if any, goes).
- Create the delete migration:
  Run: `ls apps/harness/migrations/` (confirm head `0021_runnerbinding`), then
  `uv run python manage.py makemigrations harness --name delete_sessionlink`
  Expected: `apps/harness/migrations/0022_delete_sessionlink.py` — `DeleteModel(name="SessionLink")`.

- [ ] **Step 6: Fix the other tests that referenced SessionLink**

- `git rm tests/test_harness_session_link.py` (replaced by `tests/test_harness_session_reuse.py`).
- In each of `tests/test_harness_project_session_api.py`, `tests/test_harness_project_turns.py`, `tests/test_harness_api.py`, `tests/test_harness_runner_capabilities.py`, `tests/test_harness_emdash_sessions.py`, `tests/test_mobile_loop_e2e.py`: replace assertions of the form `SessionLink.objects.get(thread_key=...)` with the `RunnerBinding.objects.get(thread_key=...)` equivalent (fields map: `live_emdash_task_id`→`session_key`, `live_runner`→`runner`, `live_host`→`host`, `summary`→`summary`, `agent_task_ext_id`→`agent_task_ext_id`). Delete assertions that a *report* creates a link (that duplicate is gone — Step 4). Run each file and fix until green:
  Run: `uv run pytest tests/test_harness_project_session_api.py tests/test_harness_project_turns.py tests/test_harness_api.py tests/test_harness_runner_capabilities.py tests/test_harness_emdash_sessions.py tests/test_mobile_loop_e2e.py -v`

- [ ] **Step 7: Run the reuse suite + full harness/sessions slice + migration check**

Run: `uv run pytest tests/test_harness_session_reuse.py tests/test_report_bindings.py -v && uv run python manage.py makemigrations --check --dry-run`
Expected: PASS; "No changes detected". Wire shape unchanged, so **no `gen:api`** this task (confirm: `git status` shows no `frontend/` change).

- [ ] **Step 8: Commit**

```bash
git add apps/harness/services.py apps/harness/models.py apps/harness/api.py apps/harness/schemas.py apps/harness/migrations/0022_delete_sessionlink.py tests/
git rm tests/test_harness_session_link.py
git commit -m "refactor(harness): fold SessionLink into RunnerBinding; delete SessionLink"
```

---

### Task 3: Attach/detach liveness — `stream_desired` flag + attach registry + REST/WS + runner signal

Model "a viewer is watching" server-side: a cache-counted attach registry (mirroring `presence.py`) flips `RunnerBinding.stream_desired` on the 0→1 / 1→0 transition and publishes a control frame to the bound runner's `runner.{id}` group. The chat WS drives it on connect/disconnect; a REST pair drives it for explicit/non-WS callers. All logic lives in a sync service so it unit-tests without Channels.

**Files:**
- Create: `apps/canopy_sessions/attach.py`
- Modify: `apps/canopy_sessions/services.py` (add `attach_session`/`detach_session`/`_set_stream_desired`)
- Modify: `apps/canopy_sessions/models.py` (`RunnerBinding.stream_desired`) + `apps/canopy_sessions/migrations/0007_runnerbinding_stream_desired.py`
- Modify: `apps/canopy_sessions/api.py` (attach/detach routes) + `apps/canopy_sessions/schemas.py` (`StreamStateOut`)
- Modify: `apps/canopy_sessions/consumers.py` (`connect`/`disconnect` wiring)
- Modify: `apps/realtime/consumers.py` (`RunnerConsumer.runner_stream` handler)
- Modify (regen): `frontend/src/api/generated.ts`
- Test: `tests/test_session_liveness.py` (new)

**Interfaces:**
- Produces: `RunnerBinding.stream_desired` (BooleanField, default False).
- Produces: `attach.attach(session_id) -> int`, `attach.detach(session_id) -> int`, `attach.count(session_id) -> int` (cache-backed count).
- Produces: `services.attach_session(session) -> bool`, `services.detach_session(session) -> bool` (returns the resulting `stream_desired`).
- Produces: `POST /api/chat/{id}/attach` and `POST /api/chat/{id}/detach` → `StreamStateOut { streaming: bool }`.
- Produces (realtime): a `{"type": "runner.stream", "session_id", "session_key", "desired"}` frame to `runner_group(binding.runner_id)`, surfaced to the runner socket as `{"type": "stream", ...}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_session_liveness.py
import pytest
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client

from apps.canopy_sessions import services
from apps.canopy_sessions.models import RunnerBinding, Session
from apps.harness.models import Runner
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()


def _bound_session(runner=True):
    ws = Workspace.objects.create(slug="w1", name="W1")
    s = Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER, title="t")
    r = None
    if runner:
        r = Runner.objects.create(name="laptop", workspace=ws, location=Runner.LOCAL)
    RunnerBinding.objects.create(session=s, runner=r, session_key="feat-x")
    return s, r


def test_attach_transition_sets_stream_desired_once(monkeypatch):
    published = []
    monkeypatch.setattr("apps.realtime.groups.publish", lambda g, m: published.append((g, m)))
    s, r = _bound_session()

    assert services.attach_session(s) is True   # 0 -> 1 : desired on
    assert services.attach_session(s) is True    # 1 -> 2 : no change
    b = RunnerBinding.objects.get(session=s)
    assert b.stream_desired is True
    # exactly one control frame on the 0->1 transition
    stream_frames = [m for _g, m in published if m.get("type") == "runner.stream"]
    assert len(stream_frames) == 1
    assert stream_frames[0]["desired"] is True
    assert stream_frames[0]["session_key"] == "feat-x"


def test_detach_to_zero_clears_stream_desired(monkeypatch):
    published = []
    monkeypatch.setattr("apps.realtime.groups.publish", lambda g, m: published.append((g, m)))
    s, r = _bound_session()
    services.attach_session(s)
    services.attach_session(s)
    assert services.detach_session(s) is True    # 2 -> 1 : still desired
    assert services.detach_session(s) is False   # 1 -> 0 : desired off
    assert RunnerBinding.objects.get(session=s).stream_desired is False
    assert published[-1][1]["desired"] is False


def test_attach_noop_without_binding(monkeypatch):
    published = []
    monkeypatch.setattr("apps.realtime.groups.publish", lambda g, m: published.append((g, m)))
    ws = Workspace.objects.create(slug="w1", name="W1")
    s = Session.objects.create(workspace=ws, origin=Session.ORIGIN_WEB, title="web-only")
    assert services.attach_session(s) is False   # no binding -> nothing to stream
    assert published == []


def test_attach_rest_endpoints_tenant_gated():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    s = Session.objects.create(workspace=ws, created_by=user, origin=Session.ORIGIN_RUNNER, title="t")
    r = Runner.objects.create(name="laptop", workspace=ws, location=Runner.LOCAL)
    RunnerBinding.objects.create(session=s, runner=r, session_key="feat-x")
    c = Client(); c.force_login(user)
    assert c.post(f"/api/chat/{s.id}/attach").json() == {"streaming": True}
    assert c.post(f"/api/chat/{s.id}/detach").json() == {"streaming": False}
    # a non-member 404s
    other = User.objects.create_user("no", "no@dimagi.com", "pw")
    c2 = Client(); c2.force_login(other)
    assert c2.post(f"/api/chat/{s.id}/attach").status_code == 404
```

Confirm the real `WorkspaceMembership` field/role names against `apps/workspaces/models.py` before running (mirror `tests/test_session_loading.py`'s `_api_ctx`).

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_session_liveness.py -v`
Expected: FAIL — `AttributeError: module 'apps.canopy_sessions.services' has no attribute 'attach_session'`.

- [ ] **Step 3: Add `stream_desired` + migration**

In `apps/canopy_sessions/models.py`, add to `RunnerBinding` (after `agent_task_ext_id`):

```python
    # Liveness: a viewer is attached, so the bound runner should stream this
    # session's events up live. Toggled by the attach registry on the 0<->1 edge.
    stream_desired = models.BooleanField(default=False)
```

Run: `uv run python manage.py makemigrations canopy_sessions --name runnerbinding_stream_desired`
Expected: `0007_runnerbinding_stream_desired.py` (AddField).

- [ ] **Step 4: Add the attach registry**

Create `apps/canopy_sessions/attach.py`:

```python
"""Cache-backed count of attached viewers per session — the "is anyone watching?"
signal that drives live streaming. Mirrors presence.py (get->mutate->set through
Django's cache), but a COUNT rather than a set: streaming stays desired while >=1
viewer is attached and stops at zero. A crashed viewer that never detaches leaves
the count high (streaming stays on) until the row is otherwise cleared — acceptable
for a single user; presence's TTL is the eventual backstop."""
from __future__ import annotations

from django.core.cache import cache

_TTL = 3600  # long; a live WS connection refreshes nothing, so keep it well above a session


def _key(session_id) -> str:
    sid = session_id.hex if hasattr(session_id, "hex") else str(session_id)
    return f"chat:attach:{sid}"


def attach(session_id) -> int:
    key = _key(session_id)
    n = int(cache.get(key) or 0) + 1
    cache.set(key, n, timeout=_TTL)
    return n


def detach(session_id) -> int:
    key = _key(session_id)
    n = max(0, int(cache.get(key) or 0) - 1)
    cache.set(key, n, timeout=_TTL)
    return n


def count(session_id) -> int:
    return int(cache.get(_key(session_id)) or 0)
```

- [ ] **Step 5: Add the attach/detach services**

In `apps/canopy_sessions/services.py`, add (near the Plan-2 helpers). Import `attach` at the top (`from . import attach`):

```python
def _set_stream_desired(session, desired: bool) -> bool:
    """Flip the bound binding's stream_desired and, on a real change, signal the
    bound runner over its control channel. Returns the resulting desired state
    (False when the session has no binding to stream)."""
    from apps.canopy_sessions.models import RunnerBinding

    binding = RunnerBinding.objects.filter(session=session).first()
    if binding is None:
        return False
    if binding.stream_desired != desired:
        binding.stream_desired = desired
        binding.save(update_fields=["stream_desired", "updated_at"])
    if binding.runner_id:
        from apps.realtime import groups
        groups.publish(groups.runner_group(binding.runner_id), {
            "type": "runner.stream",
            "session_id": str(session.id),
            "session_key": binding.session_key,
            "desired": desired,
        })
    return desired


def attach_session(session) -> bool:
    """A viewer attached. On the 0->1 edge, mark streaming desired + signal the runner."""
    n = attach.attach(session.id)
    if n == 1:
        return _set_stream_desired(session, True)
    from apps.canopy_sessions.models import RunnerBinding
    b = RunnerBinding.objects.filter(session=session).first()
    return bool(b and b.stream_desired)


def detach_session(session) -> bool:
    """A viewer detached. On the 1->0 edge, stop streaming + signal the runner."""
    n = attach.detach(session.id)
    if n == 0:
        return _set_stream_desired(session, False)
    from apps.canopy_sessions.models import RunnerBinding
    b = RunnerBinding.objects.filter(session=session).first()
    return bool(b and b.stream_desired)
```

- [ ] **Step 6: Add the REST routes + schema**

In `apps/canopy_sessions/schemas.py`, add:

```python
class StreamStateOut(Schema):
    """Whether the bound runner is being asked to stream this session live."""
    streaming: bool
```

In `apps/canopy_sessions/api.py`, import `StreamStateOut` in the schema import block, then add:

```python
@router.post("/{session_id}/attach", response=StreamStateOut, summary="Attach a viewer (start live streaming)")
def attach_session(request: HttpRequest, session_id: uuid.UUID):
    session = _session_or_404(request, session_id)
    return {"streaming": services.attach_session(session)}


@router.post("/{session_id}/detach", response=StreamStateOut, summary="Detach a viewer (stop when last leaves)")
def detach_session(request: HttpRequest, session_id: uuid.UUID):
    session = _session_or_404(request, session_id)
    return {"streaming": services.detach_session(session)}
```

- [ ] **Step 7: Wire the chat WS + the runner control frame**

In `apps/canopy_sessions/consumers.py::SessionConsumer.connect`, after `presence.touch` (`:48`) add:

```python
        await database_sync_to_async(chat_services.attach_session)(session)
```

In `disconnect` (`:52-58`), after `presence.leave` add:

```python
        await database_sync_to_async(chat_services.detach_session)(self.session)
```

In `apps/realtime/consumers.py::RunnerConsumer`, add a handler beside `runner_interject` (`:189`):

```python
    async def runner_stream(self, message):
        # runner.{id} group_send type="runner.stream" — start/stop live streaming a
        # session this runner backs. Forwarded to the runner socket; the runner also
        # syncs desired-streaming via GET /runners/{id}/streams, so a missed frame
        # only costs latency (like the wake channel).
        await self.send_json({
            "type": "stream",
            "session_id": message.get("session_id"),
            "session_key": message.get("session_key"),
            "desired": message.get("desired"),
        })
```

- [ ] **Step 8: Run tests + regen types**

Run: `uv run pytest tests/test_session_liveness.py tests/test_chat_session_consumer.py -v`
Expected: PASS (liveness cases green; the consumer suite still green — attach/detach are additive, no snapshot/frame change).

Regenerate types (new routes + `StreamStateOut`):
Run: `cd frontend && npm run gen:api && npm run build`
Expected: `generated.ts` gains the attach/detach ops + `StreamStateOut`; clean build.

- [ ] **Step 9: Commit**

```bash
git add apps/canopy_sessions/ apps/realtime/consumers.py frontend/src/api/generated.ts tests/test_session_liveness.py
git commit -m "feat(sessions): attach/detach liveness -> RunnerBinding.stream_desired + runner signal"
```

---

### Task 4: Runner stream endpoints — sync desired streams + post live events to the session group

The runner-facing half of liveness: a poll endpoint tells a runner which of its sessions to tail (the observable, un-flaky contract), and a post endpoint fans the runner's live assistant events to the session group as the SAME `chat.stream_*` frames the chat path emits (via the frozen `stream_map`). No `Message` rows written — this is live view; persistence is Task 6's backfill.

**Files:**
- Modify: `apps/harness/api.py` (add `GET /runners/{id}/streams`, `POST /runners/{id}/session-stream`)
- Modify: `apps/harness/schemas.py` (`StreamSyncOut`, `StreamDescriptorOut`, `SessionStreamIn`, `LiveEventIn`, `StreamPostOut`)
- Modify (regen): `frontend/src/api/generated.ts`
- Test: `tests/test_harness_session_streams.py` (new)

**Interfaces:**
- Consumes: `RunnerBinding.stream_desired/session_key/runner` (Tasks 1,3); `apps.realtime.groups.publish/session_group`; `stream_map`-shaped events (`{kind, seq, payload}`).
- Produces: `GET /api/harness/runners/{id}/streams` → `StreamSyncOut { streams: list[StreamDescriptorOut] }` where `StreamDescriptorOut = { session_id: str, session_key: str, project: str }` — the runner's bindings with `stream_desired=True` and a non-empty `session_key`.
- Produces: `POST /api/harness/runners/{id}/session-stream` body `SessionStreamIn { session_id: uuid, events: list[LiveEventIn] }`, `LiveEventIn = { kind: str, seq: int, payload: dict }` → `StreamPostOut { count: int }`. Gate: the runner must own the session's binding; each event is published to `session_group(session_id)` as `{"type": "chat.turn_event", "event": {...}, "turn_id": None}` (turn-less ⇒ the consumer derives `seq:<n>` message ids).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_harness_session_streams.py
import uuid
import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.canopy_sessions.models import RunnerBinding, Session
from apps.harness.models import Runner
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


def _ctx():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    runner = Runner.objects.create(name="laptop", workspace=ws, location=Runner.LOCAL,
                                   status=Runner.ONLINE, paired_by=user)
    c = Client(); c.force_login(user)
    return user, ws, runner, c


def test_streams_lists_only_desired_bindings():
    user, ws, runner, c = _ctx()
    s1 = Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER, project="echo", title="a")
    s2 = Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER, title="b")
    RunnerBinding.objects.create(session=s1, runner=runner, session_key="echo-1",
                                 stream_desired=True)
    RunnerBinding.objects.create(session=s2, runner=runner, session_key="echo-2",
                                 stream_desired=False)  # not attached -> excluded
    body = c.get(f"/api/harness/runners/{runner.id}/streams").json()
    assert [x["session_key"] for x in body["streams"]] == ["echo-1"]
    assert body["streams"][0]["session_id"] == str(s1.id)
    assert body["streams"][0]["project"] == "echo"


def test_session_stream_publishes_stream_frames(monkeypatch):
    user, ws, runner, c = _ctx()
    s = Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER, title="a")
    RunnerBinding.objects.create(session=s, runner=runner, session_key="echo-1", stream_desired=True)
    published = []
    monkeypatch.setattr("apps.realtime.groups.publish", lambda g, m: published.append((g, m)))
    body = c.post(
        f"/api/harness/runners/{runner.id}/session-stream",
        data={"session_id": str(s.id),
              "events": [{"kind": "assistant", "seq": 0, "payload": {"text": "hi"}}]},
        content_type="application/json",
    ).json()
    assert body == {"count": 1}
    assert len(published) == 1
    group, frame = published[0]
    assert group.endswith(s.id.hex)                 # the session group
    assert frame["type"] == "chat.turn_event"
    assert frame["turn_id"] is None                 # turn-less live frame
    assert frame["event"] == {"kind": "assistant", "seq": 0, "payload": {"text": "hi"}}


def test_session_stream_rejects_unbound_runner():
    user, ws, runner, c = _ctx()
    s = Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER, title="a")
    # binding belongs to a DIFFERENT runner
    other = Runner.objects.create(name="other", workspace=ws, location=Runner.LOCAL, paired_by=user)
    RunnerBinding.objects.create(session=s, runner=other, session_key="echo-1")
    resp = c.post(
        f"/api/harness/runners/{runner.id}/session-stream",
        data={"session_id": str(s.id), "events": []},
        content_type="application/json",
    )
    assert resp.status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_harness_session_streams.py -v`
Expected: FAIL — 404/route-not-found (endpoints don't exist).

- [ ] **Step 3: Add the schemas**

In `apps/harness/schemas.py`:

```python
class StreamDescriptorOut(Schema):
    session_id: str
    session_key: str
    project: str


class StreamSyncOut(Schema):
    streams: list[StreamDescriptorOut] = []


class LiveEventIn(Schema):
    kind: str
    seq: int
    payload: dict = {}


class SessionStreamIn(Schema):
    session_id: uuid.UUID
    events: list[LiveEventIn] = []


class StreamPostOut(Schema):
    count: int
```

- [ ] **Step 4: Add the routes**

In `apps/harness/api.py`, import the new schemas, then add (near the other runner routes):

```python
@router.get("/runners/{runner_id}/streams", response=StreamSyncOut)
def list_streams(request: HttpRequest, runner_id: uuid.UUID):
    """The sessions this runner should be tailing live (a viewer is attached). The
    observable half of attach/detach — the runner syncs this each tick and starts/
    stops tailers; the WS runner.stream frame is only a latency optimization."""
    from apps.canopy_sessions.models import RunnerBinding

    runner = _runner_or_404(request, runner_id)
    bindings = (
        RunnerBinding.objects.select_related("session")
        .filter(runner=runner, stream_desired=True)
        .exclude(session_key="")
    )
    return {"streams": [
        {"session_id": str(b.session_id), "session_key": b.session_key,
         "project": b.session.project}
        for b in bindings
    ]}


@router.post("/runners/{runner_id}/session-stream", response=StreamPostOut)
def post_session_stream(request: HttpRequest, runner_id: uuid.UUID, payload: SessionStreamIn):
    """The runner ships live assistant events for a session it backs; the server fans
    them to the session group as the same chat.turn_event frames the chat path uses
    (turn-less -> the consumer derives seq:<n> message ids). Live view only — no
    Message rows (that is the on-demand backfill, POST /session-backfill)."""
    from apps.canopy_sessions.models import RunnerBinding
    from apps.realtime import groups

    runner = _runner_or_404(request, runner_id)
    if not RunnerBinding.objects.filter(session_id=payload.session_id, runner=runner).exists():
        raise HttpError(404, "session not bound to this runner")
    sgroup = groups.session_group(payload.session_id)
    n = 0
    for e in payload.events:
        groups.publish(sgroup, {
            "type": "chat.turn_event",
            "event": {"kind": e.kind, "seq": e.seq, "payload": e.payload},
            "turn_id": None,
        })
        n += 1
    return {"count": n}
```

- [ ] **Step 5: Run tests + regen types**

Run: `uv run pytest tests/test_harness_session_streams.py -v`
Expected: PASS.
Run: `cd frontend && npm run gen:api && npm run build`
Expected: `generated.ts` gains the two ops + schemas; clean build.

- [ ] **Step 6: Commit**

```bash
git add apps/harness/api.py apps/harness/schemas.py frontend/src/api/generated.ts tests/test_harness_session_streams.py
git commit -m "feat(harness): runner stream sync + live-event fan-out to the session group"
```

---

### Task 5: Runner-side live streaming (`canopy_runner`)

Give the runner the streaming leg: each tick it syncs the desired streams, tails each session's transcript with `TailReader`, extracts new assistant text with `chat_bridge`, and posts it up via the Task-4 endpoint — stopping tailers for sessions no longer desired. Verified entirely with a `tmp_path` transcript + a fake client + monkeypatched transcript resolution (no laptop, no CDP).

**Files:**
- Modify: `packages/canopy_runner/canopy_runner/client.py` (`sync_streams`, `post_session_stream`)
- Modify: `packages/canopy_runner/canopy_runner/main.py` (`_stream_readers`, `_sync_session_streams`, call in `run_once`)
- Test: `packages/canopy_runner/tests/test_session_streaming.py` (new)

**Interfaces:**
- Consumes: `GET /runners/{id}/streams` (Task 4) → `{"streams": [{"session_id","session_key","project"}]}`; `POST /runners/{id}/session-stream` (Task 4); `chat_bridge.new_assistant_texts`, `TailReader`, `transcript.resolve_transcript`.
- Produces: `Client.sync_streams(runner_id) -> list[dict]`; `Client.post_session_stream(runner_id, session_id, events) -> None`; `main._sync_session_streams(cfg, client) -> None` maintaining `main._stream_readers: dict[str, dict]`.

- [ ] **Step 1: Write the failing test**

```python
# packages/canopy_runner/tests/test_session_streaming.py
"""Runner live-streaming: tail a desired session's transcript and post new assistant
text as live events; stop when it's no longer desired. Fake client + tmp transcript."""
import json

from canopy_runner import main as m


class _Cfg:
    runner_id = "r"


class _Client:
    def __init__(self, streams):
        self._streams = streams
        self.posted = []          # (session_id, events)

    def sync_streams(self, runner_id):
        return self._streams

    def post_session_stream(self, runner_id, session_id, events):
        self.posted.append((session_id, events))


def _asst(text):
    return json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}) + "\n"


def test_streams_new_assistant_text_then_stops(tmp_path, monkeypatch):
    m._stream_readers.clear()
    p = tmp_path / "echo.jsonl"
    p.write_text(_asst("old history"))  # pre-existing; seek_end skips it
    monkeypatch.setattr(m.transcript, "resolve_transcript", lambda _proj, _task, **_k: p)

    streams = [{"session_id": "s1", "session_key": "echo-1", "project": "echo"}]
    c = _Client(streams)

    # first tick: registers the tailer at end-of-file -> no events yet
    m._sync_session_streams(_Cfg(), c)
    assert c.posted == []

    # the session speaks -> next tick posts the new assistant text as a live event
    with open(p, "a") as f:
        f.write(_asst("live reply"))
    m._sync_session_streams(_Cfg(), c)
    assert len(c.posted) == 1
    sid, events = c.posted[0]
    assert sid == "s1"
    assert [e["payload"]["text"] for e in events] == ["live reply"]
    assert events[0]["kind"] == "assistant"
    assert events[0]["seq"] == 0

    # a further reply increments seq monotonically
    with open(p, "a") as f:
        f.write(_asst("more"))
    m._sync_session_streams(_Cfg(), c)
    assert c.posted[-1][1][0]["seq"] == 1

    # no longer desired -> the tailer is dropped, nothing posts
    c._streams = []
    m._sync_session_streams(_Cfg(), c)
    assert "s1" not in m._stream_readers


def test_sync_streams_survives_client_error(monkeypatch):
    m._stream_readers.clear()

    class _Boom:
        def sync_streams(self, rid):
            raise RuntimeError("network")

    m._sync_session_streams(_Cfg(), _Boom())  # must not raise
    assert m._stream_readers == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd packages/canopy_runner && uv run pytest tests/test_session_streaming.py -v`
Expected: FAIL — `AttributeError: module 'canopy_runner.main' has no attribute '_stream_readers'`.

- [ ] **Step 3: Add the client methods**

In `packages/canopy_runner/canopy_runner/client.py`, add to `Client`:

```python
    def sync_streams(self, runner_id: str) -> list[dict]:
        """The sessions a viewer is watching, which this runner should tail live."""
        _, payload = self._call("GET", f"/runners/{runner_id}/streams")
        return (payload or {}).get("streams", [])

    def post_session_stream(self, runner_id: str, session_id: str, events: list[dict]) -> None:
        """Ship live assistant events for a session this runner backs."""
        self._call("POST", f"/runners/{runner_id}/session-stream",
                   {"session_id": session_id, "events": events})
```

- [ ] **Step 4: Add the tailer manager**

In `packages/canopy_runner/canopy_runner/main.py`, near `_tail_readers` (`:186-188`), add:

```python
# Per-session live-stream tailers, keyed by session_id — active only while a viewer
# is attached (stream_desired on the server). Distinct from _tail_readers (the idle
# tail read-model that fills RunnerBinding.tail); this is the live push to attached
# viewers. Each entry: {"reader": TailReader|None, "seq": int, "session_key": str,
# "project": str}.
_stream_readers: dict[str, dict] = {}
```

Add the sync function (below `_maybe_report_sessions`):

```python
def _sync_session_streams(cfg: Config, client: Client) -> None:
    """Tail each session a viewer is watching and post new assistant text up as live
    events. Change-driven off TailReader (only newly-appended bytes), so it stays
    cheap. Best-effort — a client hiccup never breaks a tick."""
    try:
        streams = client.sync_streams(cfg.runner_id)
    except Exception:  # noqa: BLE001
        logger.debug("stream sync failed (non-fatal)", exc_info=True)
        return
    desired = {s["session_id"]: s for s in streams if s.get("session_id")}
    home = Path.home()
    claude_home = home / ".claude" / "projects"

    for sid, s in desired.items():
        if sid in _stream_readers:
            continue
        path = transcript.resolve_transcript(
            s.get("project") or "", s.get("session_key") or "", home=home, claude_home=claude_home
        )
        reader = TailReader(str(path)) if path else None
        if reader is not None:
            reader.seek_end()  # stream only NEW activity; history is loaded elsewhere
        _stream_readers[sid] = {
            "reader": reader, "seq": 0,
            "session_key": s.get("session_key") or "", "project": s.get("project") or "",
        }

    for sid in list(_stream_readers):  # drop tailers for sessions no longer watched
        if sid not in desired:
            _stream_readers.pop(sid, None)

    for sid, st in _stream_readers.items():
        reader = st["reader"]
        if reader is None:  # transcript wasn't there yet — retry resolving it
            path = transcript.resolve_transcript(
                st["project"], st["session_key"], home=home, claude_home=claude_home
            )
            if path:
                reader = TailReader(str(path)); reader.seek_end(); st["reader"] = reader
            continue
        records = reader.read_new()
        if not records:
            continue
        events = []
        for text in chat_bridge.new_assistant_texts(records, 0):
            events.append({"kind": "assistant", "seq": st["seq"], "payload": {"text": text}})
            st["seq"] += 1
        if events:
            try:
                client.post_session_stream(cfg.runner_id, sid, events)
            except Exception:  # noqa: BLE001
                logger.debug("stream post failed (non-fatal)", exc_info=True)
```

Confirm the module already imports `chat_bridge`, `transcript`, `TailReader`, `Path`, `logger`, `Config`, `Client` (it does — `main.py` uses all of them). Call it in `run_once` right after `_maybe_report_sessions(cfg, client)` (`:298`):

```python
    _maybe_report_sessions(cfg, client)
    _sync_session_streams(cfg, client)
```

- [ ] **Step 5: Run tests**

Run: `cd packages/canopy_runner && uv run pytest tests/test_session_streaming.py tests/test_session_report_live.py -v`
Expected: PASS (new streaming cases + the untouched report-live suite).

- [ ] **Step 6: Commit**

```bash
git add packages/canopy_runner/canopy_runner/client.py packages/canopy_runner/canopy_runner/main.py packages/canopy_runner/tests/test_session_streaming.py
git commit -m "feat(runner): live-stream a watched session's transcript to attached viewers"
```

---

### Task 6: On-demand backfill (server) — request → signal runner → write `Message` rows once

A local (`origin=runner`) session has no `Message` rows, only the binding tail. When the client asks for full history, the server: returns `ready` if rows already exist; else, if a live runner is bound, sets `backfill_requested` + signals the runner and returns `requested`; else returns `unavailable` (runner offline — the tail still renders). The runner (Task 7) ships history to a post endpoint that writes rows **once** and clears the flag.

**Files:**
- Modify: `apps/canopy_sessions/models.py` (`RunnerBinding.backfill_requested`) + `apps/canopy_sessions/migrations/0008_runnerbinding_backfill_requested.py`
- Modify: `apps/canopy_sessions/api.py` (`POST /{id}/backfill`) + `apps/canopy_sessions/schemas.py` (`BackfillStateOut`)
- Modify: `apps/canopy_sessions/services.py` (`request_backfill`, `write_backfill`)
- Modify: `apps/harness/api.py` (`GET /runners/{id}/backfills`, `POST /runners/{id}/session-backfill`) + `apps/harness/schemas.py` (`BackfillSyncOut`, `BackfillDescriptorOut`, `SessionBackfillIn`, `BackfillMessageIn`, `BackfillWriteOut`)
- Modify (regen): `frontend/src/api/generated.ts`
- Test: `tests/test_session_backfill.py` (new)

**Interfaces:**
- Produces: `RunnerBinding.backfill_requested` (BooleanField, default False).
- Produces: `services.request_backfill(session) -> str` returning `"ready" | "requested" | "unavailable"`; `services.write_backfill(session, messages) -> int` (writes Message rows once, chronological, returns count; no-op if rows already exist).
- Produces: `POST /api/chat/{id}/backfill` → `BackfillStateOut { status: str }`.
- Produces: `GET /api/harness/runners/{id}/backfills` → `BackfillSyncOut { backfills: list[BackfillDescriptorOut{session_id,session_key,project}] }`.
- Produces: `POST /api/harness/runners/{id}/session-backfill` body `SessionBackfillIn { session_id: uuid, messages: list[BackfillMessageIn{role,text}] }` → `BackfillWriteOut { written: int }`; runner-owned-binding gated; clears `backfill_requested`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_session_backfill.py
import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.canopy_sessions import services
from apps.canopy_sessions.models import Message, RunnerBinding, Session
from apps.harness.models import Runner
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


def _ctx(runner_online=True, has_runner=True):
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    s = Session.objects.create(workspace=ws, created_by=user, origin=Session.ORIGIN_RUNNER, title="t")
    r = None
    if has_runner:
        from apps.harness.models import Runner as R
        r = R.objects.create(name="laptop", workspace=ws, location=R.LOCAL, paired_by=user,
                             status=R.ONLINE if runner_online else R.DISCONNECTED)
        if not runner_online:
            r.last_heartbeat_at = None
    RunnerBinding.objects.create(session=s, runner=r, session_key="echo-1")
    c = Client(); c.force_login(user)
    return user, ws, s, r, c


def test_backfill_ready_when_rows_exist():
    _u, _w, s, _r, c = _ctx()
    Message.objects.create(session=s, turn_index=0, role=Message.USER, plaintext="hi")
    assert c.post(f"/api/chat/{s.id}/backfill").json() == {"status": "ready"}


def test_backfill_requested_when_runner_live(monkeypatch):
    published = []
    monkeypatch.setattr("apps.realtime.groups.publish", lambda g, m: published.append((g, m)))
    _u, _w, s, _r, c = _ctx(runner_online=True)
    assert c.post(f"/api/chat/{s.id}/backfill").json() == {"status": "requested"}
    assert RunnerBinding.objects.get(session=s).backfill_requested is True


def test_backfill_unavailable_when_no_live_runner():
    _u, _w, s, _r, c = _ctx(has_runner=False)
    assert c.post(f"/api/chat/{s.id}/backfill").json() == {"status": "unavailable"}


def test_write_backfill_writes_rows_once():
    _u, _w, s, _r, _c = _ctx()
    msgs = [{"role": "user", "text": "q1"}, {"role": "assistant", "text": "a1"}]
    assert services.write_backfill(s, msgs) == 2
    assert [m.plaintext for m in s.messages.order_by("turn_index")] == ["q1", "a1"]
    # second call is a no-op (server-full thereafter)
    assert services.write_backfill(s, msgs) == 0
    assert s.messages.count() == 2


def test_runner_backfill_endpoints(monkeypatch):
    _u, _w, s, r, c = _ctx()
    RunnerBinding.objects.filter(session=s).update(backfill_requested=True)
    # runner syncs its pending backfills
    body = c.get(f"/api/harness/runners/{r.id}/backfills").json()
    assert [b["session_id"] for b in body["backfills"]] == [str(s.id)]
    # runner ships history -> rows written, flag cleared
    resp = c.post(
        f"/api/harness/runners/{r.id}/session-backfill",
        data={"session_id": str(s.id),
              "messages": [{"role": "user", "text": "q"}, {"role": "assistant", "text": "a"}]},
        content_type="application/json",
    ).json()
    assert resp == {"written": 2}
    assert RunnerBinding.objects.get(session=s).backfill_requested is False
    assert s.messages.count() == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_session_backfill.py -v`
Expected: FAIL — `backfill_requested` field / `write_backfill` / routes don't exist.

- [ ] **Step 3: Add the field + migration**

In `apps/canopy_sessions/models.py`, add to `RunnerBinding`:

```python
    # On-demand history promotion: the client asked for full history on a local
    # session with no Message rows. The bound runner ships its transcript, the
    # server writes rows once, and clears this. (Server-full is then inferred from
    # Message existence — no second flag.)
    backfill_requested = models.BooleanField(default=False)
```

Run: `uv run python manage.py makemigrations canopy_sessions --name runnerbinding_backfill_requested`
Expected: `0008_runnerbinding_backfill_requested.py`.

- [ ] **Step 4: Add the services**

In `apps/canopy_sessions/services.py`, add (reusing `_next_index` `:95` and `_ROLE_FOR_KIND`-style role handling — but backfill carries an explicit `role` string):

```python
_BACKFILL_ROLES = {Message.USER, Message.ASSISTANT, Message.TOOL_USE, Message.TOOL_RESULT, Message.SYSTEM}


def request_backfill(session) -> str:
    """The client asked for full history. 'ready' if already server-full; 'requested'
    if a live runner is bound (signal it); 'unavailable' otherwise (tail still shows)."""
    from apps.canopy_sessions.models import RunnerBinding
    from apps.harness.models import Runner

    if session.messages.exists():
        return "ready"
    binding = RunnerBinding.objects.select_related("runner").filter(session=session).first()
    if binding is None or binding.runner_id is None or binding.runner.live_status != Runner.ONLINE:
        return "unavailable"
    if not binding.backfill_requested:
        binding.backfill_requested = True
        binding.save(update_fields=["backfill_requested", "updated_at"])
    from apps.realtime import groups
    groups.publish(groups.runner_group(binding.runner_id), {
        "type": "runner.stream",  # reuse the control frame; desired=None marks a backfill ask
        "session_id": str(session.id), "session_key": binding.session_key, "desired": None,
    })
    return "requested"


def write_backfill(session, messages) -> int:
    """Write a runner's shipped transcript as Message rows — ONCE. No-op if the
    session already has rows (server-full). messages: [{"role","text"}] chronological."""
    with transaction.atomic():
        locked = Session.objects.select_for_update().get(pk=session.pk)
        if Message.objects.filter(session=locked).exists():
            return 0
        index = _next_index(locked)
        written = 0
        for msg in messages:
            role = msg.get("role")
            if role not in _BACKFILL_ROLES:
                continue
            Message.objects.create(
                session=locked, turn_index=index, role=role,
                content={"text": msg.get("text", ""), "backfill": True},
                plaintext=str(msg.get("text", "")),
            )
            index += 1
            written += 1
    return written
```

- [ ] **Step 5: Add the client-facing route + schema**

In `apps/canopy_sessions/schemas.py`:

```python
class BackfillStateOut(Schema):
    """ready = already server-full; requested = runner asked; unavailable = offline."""
    status: str
```

In `apps/canopy_sessions/api.py`:

```python
@router.post("/{session_id}/backfill", response=BackfillStateOut, summary="Request full history from the runner")
def request_backfill(request: HttpRequest, session_id: uuid.UUID):
    session = _session_or_404(request, session_id)
    return {"status": services.request_backfill(session)}
```

- [ ] **Step 6: Add the runner-facing routes + schemas**

In `apps/harness/schemas.py`:

```python
class BackfillDescriptorOut(Schema):
    session_id: str
    session_key: str
    project: str


class BackfillSyncOut(Schema):
    backfills: list[BackfillDescriptorOut] = []


class BackfillMessageIn(Schema):
    role: str
    text: str = ""


class SessionBackfillIn(Schema):
    session_id: uuid.UUID
    messages: list[BackfillMessageIn] = []


class BackfillWriteOut(Schema):
    written: int
```

In `apps/harness/api.py`:

```python
@router.get("/runners/{runner_id}/backfills", response=BackfillSyncOut)
def list_backfills(request: HttpRequest, runner_id: uuid.UUID):
    """Sessions this runner has been asked to ship full history for."""
    from apps.canopy_sessions.models import RunnerBinding

    runner = _runner_or_404(request, runner_id)
    bindings = (
        RunnerBinding.objects.select_related("session")
        .filter(runner=runner, backfill_requested=True)
    )
    return {"backfills": [
        {"session_id": str(b.session_id), "session_key": b.session_key,
         "project": b.session.project}
        for b in bindings
    ]}


@router.post("/runners/{runner_id}/session-backfill", response=BackfillWriteOut)
def post_session_backfill(request: HttpRequest, runner_id: uuid.UUID, payload: SessionBackfillIn):
    """The runner ships a session's full transcript; the server writes Message rows
    once and clears the request. Runner-owned-binding gated."""
    from apps.canopy_sessions.models import RunnerBinding, Session
    from apps.canopy_sessions import services as chat_services

    runner = _runner_or_404(request, runner_id)
    binding = RunnerBinding.objects.filter(session_id=payload.session_id, runner=runner).first()
    if binding is None:
        raise HttpError(404, "session not bound to this runner")
    session = Session.objects.get(pk=payload.session_id)
    written = chat_services.write_backfill(session, [m.dict() for m in payload.messages])
    binding.backfill_requested = False
    binding.save(update_fields=["backfill_requested", "updated_at"])
    return {"written": written}
```

- [ ] **Step 7: Run tests + regen types**

Run: `uv run pytest tests/test_session_backfill.py -v`
Expected: PASS.
Run: `cd frontend && npm run gen:api && npm run build`
Expected: `generated.ts` gains the backfill ops + schemas; clean build.

- [ ] **Step 8: Commit**

```bash
git add apps/canopy_sessions/ apps/harness/api.py apps/harness/schemas.py frontend/src/api/generated.ts tests/test_session_backfill.py
git commit -m "feat(sessions): on-demand backfill — request/signal/ship/write-once + runner-offline unavailable"
```

---

### Task 7: Runner-side backfill (`canopy_runner`)

Give the runner the backfill leg: each tick it drains pending backfills, reads each session's full transcript, maps it to `{role, text}` messages, and ships them via the Task-6 endpoint. Verified with a `tmp_path` transcript + fake client (no laptop).

**Files:**
- Modify: `packages/canopy_runner/canopy_runner/client.py` (`sync_backfills`, `post_session_backfill`)
- Modify: `packages/canopy_runner/canopy_runner/chat_bridge.py` (`transcript_messages`)
- Modify: `packages/canopy_runner/canopy_runner/main.py` (`_drain_backfills`, call in `run_once`)
- Test: `packages/canopy_runner/tests/test_session_backfill_runner.py` (new); append to `tests/test_chat_bridge.py`

**Interfaces:**
- Consumes: `GET /runners/{id}/backfills` (Task 6) → `{"backfills": [{"session_id","session_key","project"}]}`; `POST /runners/{id}/session-backfill` (Task 6); `chat_bridge.read_records`, `transcript.resolve_transcript`.
- Produces: `chat_bridge.transcript_messages(records) -> list[dict]` (user + assistant text rows, chronological); `Client.sync_backfills(runner_id) -> list[dict]`; `Client.post_session_backfill(runner_id, session_id, messages) -> None`; `main._drain_backfills(cfg, client) -> None`.

- [ ] **Step 1: Write the failing test**

```python
# packages/canopy_runner/tests/test_session_backfill_runner.py
"""Runner backfill: read a session's full transcript and ship it as {role,text}
messages when the server asks. Fake client + tmp transcript."""
import json

from canopy_runner import main as m


class _Cfg:
    runner_id = "r"


class _Client:
    def __init__(self, backfills):
        self._backfills = backfills
        self.shipped = []  # (session_id, messages)

    def sync_backfills(self, runner_id):
        return self._backfills

    def post_session_backfill(self, runner_id, session_id, messages):
        self.shipped.append((session_id, messages))


def _asst(t):
    return json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": t}]}}) + "\n"


def _user(t):
    return json.dumps({"type": "user", "message": {"content": t}}) + "\n"


def test_drains_backfill_and_ships_full_transcript(tmp_path, monkeypatch):
    p = tmp_path / "echo.jsonl"
    p.write_text(_user("q1") + _asst("a1") + _user("q2") + _asst("a2"))
    monkeypatch.setattr(m.transcript, "resolve_transcript", lambda _proj, _task, **_k: p)

    c = _Client([{"session_id": "s1", "session_key": "echo-1", "project": "echo"}])
    m._drain_backfills(_Cfg(), c)

    assert len(c.shipped) == 1
    sid, messages = c.shipped[0]
    assert sid == "s1"
    assert [(x["role"], x["text"]) for x in messages] == [
        ("user", "q1"), ("assistant", "a1"), ("user", "q2"), ("assistant", "a2"),
    ]


def test_drain_skips_when_transcript_missing(monkeypatch):
    monkeypatch.setattr(m.transcript, "resolve_transcript", lambda *a, **k: None)
    c = _Client([{"session_id": "s1", "session_key": "echo-1", "project": "echo"}])
    m._drain_backfills(_Cfg(), c)
    assert c.shipped == []  # nothing to ship; runner-offline path stays server-side
```

```python
# packages/canopy_runner/tests/test_chat_bridge.py  (append)
def test_transcript_messages_maps_user_and_assistant():
    from canopy_runner.chat_bridge import transcript_messages
    recs = [_user("q1"), _asst("a1"), _tool(), _asst("a2")]
    assert transcript_messages(recs) == [
        {"role": "user", "text": "q1"},
        {"role": "assistant", "text": "a1"},
        {"role": "assistant", "text": "a2"},  # tool_use block skipped (no text)
    ]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd packages/canopy_runner && uv run pytest tests/test_session_backfill_runner.py tests/test_chat_bridge.py -k "backfill or transcript_messages" -v`
Expected: FAIL — `transcript_messages` / `_drain_backfills` don't exist.

- [ ] **Step 3: Add `transcript_messages` to `chat_bridge`**

In `packages/canopy_runner/canopy_runner/chat_bridge.py`, add (reusing `_assistant_text`):

```python
def _user_text(content) -> str:
    """A user record's text — a bare string, or the text blocks of a content list."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ).strip()
    return ""


def transcript_messages(records: list[dict]) -> list[dict]:
    """The full transcript as chronological {"role","text"} rows — user + assistant
    text only (tool blocks skipped, matching the v1 bridge). Drives on-demand
    backfill of a local session's history into server Message rows."""
    out: list[dict] = []
    for rec in records:
        kind = rec.get("type")
        msg = rec.get("message")
        content = msg.get("content", "") if isinstance(msg, dict) else ""
        if kind == "user":
            t = _user_text(content)
            if t:
                out.append({"role": "user", "text": t})
        elif kind == "assistant":
            t = _assistant_text(content)
            if t:
                out.append({"role": "assistant", "text": t})
    return out
```

- [ ] **Step 4: Add the client methods**

In `packages/canopy_runner/canopy_runner/client.py`, add to `Client`:

```python
    def sync_backfills(self, runner_id: str) -> list[dict]:
        """Sessions the server asked this runner to ship full history for."""
        _, payload = self._call("GET", f"/runners/{runner_id}/backfills")
        return (payload or {}).get("backfills", [])

    def post_session_backfill(self, runner_id: str, session_id: str, messages: list[dict]) -> None:
        """Ship a session's full transcript for the server to write as Message rows."""
        self._call("POST", f"/runners/{runner_id}/session-backfill",
                   {"session_id": session_id, "messages": messages})
```

- [ ] **Step 5: Add the drain to `main.py`**

In `packages/canopy_runner/canopy_runner/main.py`, add (below `_sync_session_streams`):

```python
def _drain_backfills(cfg: Config, client: Client) -> None:
    """Ship full transcript history for each session the server asked to backfill.
    Best-effort — a missing transcript or a client hiccup is skipped, not fatal."""
    try:
        backfills = client.sync_backfills(cfg.runner_id)
    except Exception:  # noqa: BLE001
        logger.debug("backfill sync failed (non-fatal)", exc_info=True)
        return
    home = Path.home()
    claude_home = home / ".claude" / "projects"
    for b in backfills:
        sid = b.get("session_id")
        path = transcript.resolve_transcript(
            b.get("project") or "", b.get("session_key") or "", home=home, claude_home=claude_home
        )
        if not (sid and path):
            continue  # transcript not resolvable -> leave it; server keeps showing the tail
        messages = chat_bridge.transcript_messages(chat_bridge.read_records(path))
        try:
            client.post_session_backfill(cfg.runner_id, sid, messages)
        except Exception:  # noqa: BLE001
            logger.debug("backfill post failed (non-fatal)", exc_info=True)
```

Call it in `run_once` after `_sync_session_streams(cfg, client)`:

```python
    _sync_session_streams(cfg, client)
    _drain_backfills(cfg, client)
```

- [ ] **Step 6: Run tests**

Run: `cd packages/canopy_runner && uv run pytest tests/test_session_backfill_runner.py tests/test_chat_bridge.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add packages/canopy_runner/canopy_runner/client.py packages/canopy_runner/canopy_runner/chat_bridge.py packages/canopy_runner/canopy_runner/main.py packages/canopy_runner/tests/test_session_backfill_runner.py packages/canopy_runner/tests/test_chat_bridge.py
git commit -m "feat(runner): ship full transcript history on server backfill request"
```

---

### Task 8: Full-suite, boundary, migration & type-freshness guard

A cheap guard task: confirm both suites are green, the framework boundary is intact after the fold, no migration is pending, and `generated.ts` is fresh (the `regen-openapi` gate).

**Files:** none (verification only).

- [ ] **Step 1: Backend suite**

Run: `uv run pytest -q`
Expected: PASS. If a `SessionLink` reference the greps missed surfaces, fix it (it's gone).

- [ ] **Step 2: Runner suite**

Run: `cd packages/canopy_runner && uv run pytest -q`
Expected: PASS.

- [ ] **Step 3: Boundary + no-pending-migration**

Run: `uv run pytest tests/test_architecture_boundary.py -v && uv run python manage.py makemigrations --check --dry-run`
Expected: boundary PASS (no framework→product import — all new code is in `canopy_sessions`/`harness`/`realtime`, all framework); "No changes detected".

- [ ] **Step 4: Type freshness (the CI gate)**

Run: `cd frontend && npm run gen:api && git diff --exit-code src/api/generated.ts`
Expected: exit 0 (already committed in Tasks 3, 4, 6). A non-empty diff means a schema commit was missed — commit it. Then `npm run build` → clean.

- [ ] **Step 5: Commit (only if a residual fix surfaced)**

```bash
git add -A
git commit -m "test: full-suite + boundary + type-freshness green for Plan 3 liveness"
```

---

## Self-Review

**Spec coverage (design §2, §4, §7 + Open questions):**
- **SessionLink fold** (§ Design 1, § Migration 8: "fold `SessionLink` into the binding") → Tasks 1 (fields + `reusable_by` + partial unique) + 2 (services rewrite + delete). The partial unique `(runner, session_key)` Plan 1's review deferred → Task 1. ✅
- **Attach/detach liveness, server-side** (§4: "while ≥1 viewer is attached … stream; when the last viewer detaches … stops") → Task 3 (`stream_desired` + cache-counted attach registry + REST/WS + runner signal). ✅
- **Runner streaming on attach** (§7: "stream that session's transcript events up live; stop when it has none") → Task 4 (server sync + fan-out endpoints) + Task 5 (runner tailer, start/stop by desired). Reuses `TailReader` + `chat_bridge` + the frozen `stream_map`/`chat.stream_*` protocol. ✅
- **On-demand backfill** (§2: "ask the bound runner to ship history … written as `Message` rows once … server-full thereafter … Runner offline → 'full history unavailable'") → Task 6 (request/`ready`/`requested`/`unavailable` + write-once + runner routes) + Task 7 (runner reads transcript + ships). ✅
- **`session_key` re-binding open question** ("how a re-bound session (runner A → B) reconciles its `runner_binding` without losing the tail") → resolved in Task 2: `record_session` re-points `runner`/`host`/`session_key` on the SAME binding row (keyed by `(target, thread_key)`), so `tail`/`summary` survive a runner change; `test_record_is_idempotent_per_thread` covers the re-point. ✅
- **Deferred by design (stated, not built):** the whole frontend surface + the `/api/chat`→`/api/sessions` URL rename (Plan 4); cloud full-transcript persistence (already exists). ✅

**Test-surface map (every task green IN THIS REPO):**
- Backend `uv run pytest`: Tasks 1, 2, 3, 4, 6, 8.
- Runner `cd packages/canopy_runner && uv run pytest`: Tasks 5, 7 (fake client + `tmp_path` transcript + monkeypatched resolve — no laptop/CDP/DB).
- No task's only verification is "deploy and watch." The one genuinely un-unit-testable seam — the WS `runner.stream` push reaching a *real* runner socket — is deliberately made a latency optimization over the poll endpoint (`GET /runners/{id}/streams`), which IS unit-tested (Task 4) and IS what the runner acts on (Task 5). The `RunnerConsumer.runner_stream` handler is a 6-line forwarder mirroring the already-shipped `runner_interject`.

**Placeholder scan:** No TBD/TODO. Every code step shows real code grounded in read signatures. The Task-2 Step-6 test edits ("replace `SessionLink.objects.get` with `RunnerBinding.objects.get`") name the exact field mapping rather than hand-waving. The `WorkspaceMembership` field names are flagged for confirmation against the real model (mirroring Plan 2's `_api_ctx`), not guessed silently.

**Type consistency:**
- `RunnerBinding` field names are stable across tasks: `thread_key`/`host`/`agent_task_ext_id` (T1), `stream_desired` (T3), `backfill_requested` (T6); `session_key`/`tail`/`summary` (Plan 1).
- `resolve_session`/`record_session`/`replace_reported_sessions` keep Plan 1's exact signatures (T2) — the runner routes and `execute.py` client calls are untouched, and `ResolveSessionOut` stays frozen (no `gen:api` T1/T2).
- Server↔runner contracts match: `StreamSyncOut.streams[*] = {session_id, session_key, project}` (T4) is exactly what `main._sync_session_streams` reads (T5); `SessionStreamIn.events[*] = {kind, seq, payload}` (T4) is exactly what the runner posts (T5). `BackfillSyncOut.backfills[*] = {session_id, session_key, project}` (T6) ↔ `_drain_backfills` reader (T7); `SessionBackfillIn.messages[*] = {role, text}` (T6) ↔ `transcript_messages` output (T7).
- The live fan-out frame `{"type":"chat.turn_event","event":{kind,seq,payload},"turn_id":None}` (T4) is exactly the shape `SessionConsumer.chat_turn_event` consumes (`consumers.py:190`), and `turn_id=None` routes to the `f"seq:{seq}"` message-id branch (`_resolve_message_id_sync`) — no UUID coercion error.

## Notes for the implementer

- **Verify migration numbers first:** `ls apps/canopy_sessions/migrations/ apps/harness/migrations/`. The plan assumes canopy_sessions head `0005` (→ `0006`/`0007`/`0008`) and harness head `0021` (→ `0022`). Adjust `--name` file numbers if newer migrations exist.
- **Confirm the real `WorkspaceMembership` create signature** (`apps/workspaces/models.py`) before running the Task 3/4/6 API tests — the plan mirrors Plan 2's `_api_ctx` but the exact field/role constants must match (Plan 2 used `WorkspaceMembership.OWNER`).
- **`gen:api` runs on Tasks 3, 4, 6 only** — each must commit `frontend/src/api/generated.ts` in the same commit as its schema change, or `regen-openapi` CI fails the PR. Tasks 1 & 2 keep the wire frozen (fold changes storage only) — confirm `git status` shows no `frontend/` diff there.
- **Delete `SessionLink` LAST (Task 2), not Task 1** — Task 1 only adds binding fields (nothing reads them yet), so the suite stays green while `services.py` still writes `SessionLink`. Task 2 flips the services and drops the model in one commit.
- **The attach registry's crashed-viewer caveat:** a viewer that never detaches leaves the count > 0 (streaming stays on) until the row is otherwise cleared. Acceptable for a single user; a TTL-tied-to-presence hardening is a later concern (noted in `attach.py`).
- **URL prefixes are unchanged this plan** — the routers still mount at `/api/chat` and `/api/harness`, and the chat WS stays `ws/chat/{id}/` (renamed with the frontend in Plan 4), so the SPA keeps working after each task.
- **The runner needs a version bump + redeploy** to ship Tasks 5/7 (launchd `com.canopy.runner`) — but that is an operational step AFTER this plan's green suite, not a task here; nothing in Plan 3 depends on a live runner to verify.
