# Unified Runner Sessions — Plan 1: Model Unification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collapse the harness `EmdashSession` read-model into a first-class `RunnerBinding` on the chat `Session`, make the runner (location/engine) first-class, and rename `apps/chat` → `apps/sessions` — while preserving the existing `EmdashSessionOut` wire contract so the frontend keeps working unchanged.

**Architecture:** One `Session` (evolved from `apps/chat.Session`, gains `origin`) is the canonical "canopy runner session." A one-to-one `RunnerBinding` (new, in the same app) absorbs `EmdashSession`'s tail read-model; `Runner` gains `location` (local/cloud) + `engine`. The runner's session report now upserts a lightweight `Session(origin="runner")` + its `RunnerBinding` instead of an `EmdashSession` row. `list_visible_sessions` and the realtime fan-out are rewritten to serialize the SAME `EmdashSessionOut` shape from the new model, so no frontend change is needed in this plan.

**Tech Stack:** Django 5 ASGI, Django-Ninja + Pydantic v2, Django Channels, PostgreSQL, pytest.

## Global Constraints

- **No backwards compatibility, no data preservation.** Single user; existing `chat_*` / `EmdashSession` rows may be wiped. Migrations may drop data.
- **`SessionLink` is NOT folded in this plan.** It stays as-is (runner-reuse resolve/record path); its merge into `RunnerBinding` happens in Plan 3 alongside the runner protocol changes. This plan only absorbs `EmdashSession` (the tail read-model).
- **Wire contract is frozen this plan.** `EmdashSessionOut` (schema fields `id, emdash_task, project, status, last_interacted_at, recent_messages, workspace, runner_name`) and the `supervisor.sessions` frame keep their exact shape. The frontend (`OpenSessions.tsx`, `harness.ts`, `useLiveSupervisor.ts`) is untouched here — it changes in Plan 4.
- **Framework boundary holds.** `apps/sessions` and `apps/harness` are framework apps; no framework→product imports. `tests/test_architecture_boundary.py` must stay green.
- **Every schema/api change regenerates types:** `cd frontend && npm run gen:api` (backend up) — but this plan aims for a no-op diff to `generated.ts` since the wire shape is frozen.
- Run backend tests with `uv run pytest`. Run one test: `uv run pytest tests/path::name -v`.

## ⚠️ Cutover note (deploy safety) — added post-implementation

The app was renamed `apps/chat` → `apps/canopy_sessions` (module + Django label; plain `sessions` collides with `django.contrib.sessions`) by repointing the historical migrations in place — there is **no** Django-native `AlterModelTable` / `SeparateDatabaseAndState` transition from the old `chat` label. Consequences:

- **Fresh DB (tests, new dev):** correct — a fresh migrate builds `canopy_sessions_*` tables under the new label. This is what the green suite validates.
- **Existing labs DB (the trap):** `django_migrations` holds rows under app label `chat`, so `manage.py migrate` treats `canopy_sessions` as brand-new and applies `0001–0005` fresh: it creates **empty** `canopy_sessions_*` tables, leaves the old `chat_*` tables orphaned, and leaves `harness.Turn.chat_session`'s FK still pointing at the stale `chat_session` table while the ORM writes into `canopy_sessions_session`. The deploy-to-labs auto-migrate does **not** fail loudly — it produces a silent inconsistent schema.

Because data is disposable / single-user ("fine losing historical chats"), the intended cutover is an **explicit DB reset** on labs (DROP + recreate the `canopy_web` DB, or reset `django_migrations`) as part of the deploy that first ships this — NOT a reliance on the idempotent auto-migrate. Alternatively, ship a `SeparateDatabaseAndState`/`AlterModelTable` migration that renames `chat_* → canopy_sessions_*` and reconciles the label if a zero-downtime, data-preserving cutover is ever wanted. (Whole-branch review finding #1.)

---

### Task 1: `Runner` gains `location` + `engine`

Make the runner first-class about its environment. `location` is the persistence-tier discriminator (cloud → full transcript later; local → tail-only). `engine` decouples from "emdash". The existing `kind` (EMDASH/CLOUD/REMOTE) is left in place for current routing/capabilities and is not the tier signal.

**Files:**
- Modify: `apps/harness/models.py` (the `Runner` class, ~`:23-105`)
- Create: `apps/harness/migrations/0014_runner_location_engine.py` (next number — verify with `ls apps/harness/migrations/`)
- Test: `tests/test_harness_runner_environment.py`

**Interfaces:**
- Produces: `Runner.location` (str, `"local"|"cloud"`, default `"local"`), `Runner.engine` (str, default `"emdash"`), and classmethod-free constants `Runner.LOCAL="local"`, `Runner.CLOUD="cloud"`, `Runner.ENGINE_EMDASH="emdash"`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_harness_runner_environment.py
import pytest
from apps.harness.models import Runner

pytestmark = pytest.mark.django_db


def test_runner_defaults_local_emdash():
    r = Runner.objects.create(name="laptop")
    assert r.location == Runner.LOCAL
    assert r.engine == Runner.ENGINE_EMDASH


def test_runner_can_be_cloud():
    r = Runner.objects.create(name="cloud-1", location=Runner.CLOUD)
    assert r.location == Runner.CLOUD
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_harness_runner_environment.py -v`
Expected: FAIL — `AttributeError: type object 'Runner' has no attribute 'LOCAL'`.

- [ ] **Step 3: Add the fields + constants to `Runner`**

In `apps/harness/models.py`, inside the `Runner` class near the other choice constants (above `kind`), add:

```python
    # Environment (first-class; the persistence tier derives from `location`).
    LOCAL = "local"
    CLOUD = "cloud"
    LOCATION_CHOICES = [(LOCAL, "Local"), (CLOUD, "Cloud")]
    ENGINE_EMDASH = "emdash"
    ENGINE_CHOICES = [(ENGINE_EMDASH, "emdash")]

    location = models.CharField(
        max_length=16, choices=LOCATION_CHOICES, default=LOCAL,
        help_text="Where the runner runs. Drives the session persistence tier.",
    )
    engine = models.CharField(
        max_length=32, choices=ENGINE_CHOICES, default=ENGINE_EMDASH,
        help_text="The agent engine this runner drives (not assumed to be emdash).",
    )
```

- [ ] **Step 4: Generate the migration**

Run: `uv run python manage.py makemigrations harness --name runner_location_engine`
Expected: creates `apps/harness/migrations/0014_runner_location_engine.py` adding two fields.

- [ ] **Step 5: Run tests + migrate check**

Run: `uv run pytest tests/test_harness_runner_environment.py -v && uv run python manage.py makemigrations --check --dry-run`
Expected: PASS, and `--check` reports "No changes detected".

- [ ] **Step 6: Commit**

```bash
git add apps/harness/models.py apps/harness/migrations/0014_runner_location_engine.py tests/test_harness_runner_environment.py
git commit -m "feat(harness): Runner.location + engine (first-class environment)"
```

---

### Task 2: `Session` gains `origin`

Provenance: was the session started in-app (`web`) or discovered on a runner (`runner`). Independent of which runner backs it.

**Files:**
- Modify: `apps/chat/models.py` (the `Session` class, `:18-62`)
- Create: `apps/chat/migrations/0004_session_origin.py`
- Test: `tests/test_chat_models.py` (append)

**Interfaces:**
- Produces: `Session.origin` (str, `"web"|"runner"`, default `"web"`), constants `Session.ORIGIN_WEB="web"`, `Session.ORIGIN_RUNNER="runner"`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_chat_models.py  (append)
def test_session_origin_defaults_web(db):
    from apps.chat.models import Session
    from apps.workspaces.models import Workspace
    ws = Workspace.objects.create(slug="w1", name="W1")
    s = Session.objects.create(workspace=ws, title="t")
    assert s.origin == Session.ORIGIN_WEB
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_chat_models.py::test_session_origin_defaults_web -v`
Expected: FAIL — `AttributeError: ... 'Session' has no attribute 'ORIGIN_WEB'`.

- [ ] **Step 3: Add the field**

In `apps/chat/models.py`, inside `Session` near `status`:

```python
    ORIGIN_WEB = "web"
    ORIGIN_RUNNER = "runner"
    ORIGIN_CHOICES = [(ORIGIN_WEB, "Web"), (ORIGIN_RUNNER, "Runner")]
    origin = models.CharField(max_length=10, choices=ORIGIN_CHOICES, default=ORIGIN_WEB)
```

- [ ] **Step 4: Generate the migration**

Run: `uv run python manage.py makemigrations chat --name session_origin`
Expected: `apps/chat/migrations/0004_session_origin.py`.

- [ ] **Step 5: Run test**

Run: `uv run pytest tests/test_chat_models.py::test_session_origin_defaults_web -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/chat/models.py apps/chat/migrations/0004_session_origin.py tests/test_chat_models.py
git commit -m "feat(chat): Session.origin (web|runner) provenance"
```

---

### Task 3: `RunnerBinding` model + drop `EmdashSession`

The one-to-one binding absorbs `EmdashSession`'s tail read-model. It lives in `apps/chat` (imports `harness.Runner` by string ref — both framework apps).

**Files:**
- Modify: `apps/chat/models.py` (add `RunnerBinding` at end)
- Modify: `apps/harness/models.py` (delete the `EmdashSession` class, `:441-470`)
- Create: `apps/chat/migrations/0005_runnerbinding.py`
- Create: `apps/harness/migrations/0015_drop_emdashsession.py`
- Test: `tests/test_runner_binding.py`

**Interfaces:**
- Produces: `apps.chat.models.RunnerBinding` with fields:
  `session` (OneToOne→`chat.Session`, CASCADE, related_name `"runner_binding"`),
  `runner` (FK→`harness.Runner`, SET_NULL, null, related_name `"session_bindings"`),
  `session_key` (CharField 255),
  `tail` (JSONField default=list),
  `summary` (TextField blank default ""),
  `status` (CharField 40 blank default ""),
  `last_interacted_at` (DateTimeField null),
  `live_seen_at` (DateTimeField null),
  `updated_at` (auto_now).
- Removes: `apps.harness.models.EmdashSession`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_runner_binding.py
import pytest
from apps.chat.models import Session, RunnerBinding
from apps.harness.models import Runner
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db


def test_binding_is_one_to_one_and_absorbs_tail():
    ws = Workspace.objects.create(slug="w1", name="W1")
    runner = Runner.objects.create(name="laptop", workspace=ws, location=Runner.LOCAL)
    session = Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER, title="feat-x")
    b = RunnerBinding.objects.create(
        session=session, runner=runner, session_key="feat-x",
        tail=[{"role": "assistant", "text": "hi"}], summary="rolling",
    )
    assert session.runner_binding == b
    assert b.tail[0]["text"] == "hi"


def test_emdashsession_is_gone():
    import apps.harness.models as m
    assert not hasattr(m, "EmdashSession")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_runner_binding.py -v`
Expected: FAIL — `ImportError: cannot import name 'RunnerBinding'`.

- [ ] **Step 3: Add `RunnerBinding` to `apps/chat/models.py`**

Append:

```python
class RunnerBinding(models.Model):
    """The live pointer from a Session to the runner currently backing it, plus
    the cheap tail read-model. Absorbs the old harness.EmdashSession. Null when
    nothing is live for the session."""

    session = models.OneToOneField(
        Session, on_delete=models.CASCADE, related_name="runner_binding"
    )
    runner = models.ForeignKey(
        "harness.Runner", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="session_bindings",
    )
    # Engine-agnostic handle the runner uses to resume/inject (was emdash_task).
    session_key = models.CharField(max_length=255)
    tail = models.JSONField(default=list)          # last N conversational messages
    summary = models.TextField(blank=True, default="")
    status = models.CharField(max_length=40, blank=True, default="")
    last_interacted_at = models.DateTimeField(null=True, blank=True)
    live_seen_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-last_interacted_at"]

    def __str__(self) -> str:
        return f"binding<{self.session_key}>"
```

- [ ] **Step 4: Delete `EmdashSession` from `apps/harness/models.py`**

Remove the entire `class EmdashSession(...)` block (`:441-470`). Leave `SessionLink` and `Runner` intact.

- [ ] **Step 5: Generate both migrations**

Run: `uv run python manage.py makemigrations chat harness --name runnerbinding`
Expected: `apps/chat/migrations/0005_runnerbinding.py` (CreateModel RunnerBinding, depends on `harness` for the Runner FK) and `apps/harness/migrations/0015_...` (DeleteModel EmdashSession). If Django names them awkwardly, rename files + update `name=`.

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/test_runner_binding.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add apps/chat/models.py apps/harness/models.py apps/chat/migrations/0005_runnerbinding.py apps/harness/migrations/0015_*.py tests/test_runner_binding.py
git commit -m "feat: RunnerBinding absorbs EmdashSession tail read-model"
```

---

### Task 4: Report path upserts `Session(origin=runner)` + `RunnerBinding`

Rewrite `replace_reported_sessions` to stop creating `EmdashSession` rows. For each reported runner session it upserts a durable `Session(origin="runner")` keyed by `(runner, session_key)` and refreshes its `RunnerBinding` (tail, status, times). Sessions that fell off the report are NOT deleted (they're durable now) — their binding's `runner`/`live_seen_at` are cleared so the "currently open" view (Task 5) drops them.

**Files:**
- Modify: `apps/harness/services.py` (`replace_reported_sessions`, `:654-712`)
- Test: `tests/test_harness_emdash_sessions.py` (rewrite the report assertions) + new `tests/test_report_bindings.py`

**Interfaces:**
- Consumes: `RunnerBinding`, `Session` (Task 2/3); `ReportedSessionIn` (unchanged: `emdash_task, project, status, last_interacted_at, recent_messages`).
- Produces: `replace_reported_sessions(runner, workspace, sessions) -> int` — same signature; now writes `Session`+`RunnerBinding`. A reported `emdash_task` maps to `RunnerBinding.session_key`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_report_bindings.py
import pytest
from types import SimpleNamespace
from apps.harness.services import replace_reported_sessions
from apps.harness.models import Runner
from apps.chat.models import Session, RunnerBinding
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db


def _reported(task, msgs):
    return SimpleNamespace(
        emdash_task=task, project="canopy-web", status="running",
        last_interacted_at=None, recent_messages=msgs,
    )


def test_report_creates_session_and_binding():
    ws = Workspace.objects.create(slug="w1", name="W1")
    runner = Runner.objects.create(name="laptop", workspace=ws, location=Runner.LOCAL)
    n = replace_reported_sessions(runner, ws, [_reported("feat-x", [{"role": "assistant", "text": "hi"}])])
    assert n == 1
    b = RunnerBinding.objects.get(runner=runner, session_key="feat-x")
    assert b.session.origin == Session.ORIGIN_RUNNER
    assert b.tail == [{"role": "assistant", "text": "hi"}]


def test_report_is_idempotent_and_updates_tail():
    ws = Workspace.objects.create(slug="w1", name="W1")
    runner = Runner.objects.create(name="laptop", workspace=ws, location=Runner.LOCAL)
    replace_reported_sessions(runner, ws, [_reported("feat-x", [{"role": "user", "text": "a"}])])
    replace_reported_sessions(runner, ws, [_reported("feat-x", [{"role": "assistant", "text": "b"}])])
    assert Session.objects.filter(runner_binding__session_key="feat-x").count() == 1
    b = RunnerBinding.objects.get(runner=runner, session_key="feat-x")
    assert b.tail == [{"role": "assistant", "text": "b"}]


def test_dropped_session_clears_live_but_keeps_session():
    ws = Workspace.objects.create(slug="w1", name="W1")
    runner = Runner.objects.create(name="laptop", workspace=ws, location=Runner.LOCAL)
    replace_reported_sessions(runner, ws, [_reported("feat-x", [])])
    replace_reported_sessions(runner, ws, [])  # feat-x no longer open
    b = RunnerBinding.objects.get(session_key="feat-x")
    assert b.runner_id is None       # live pointer cleared
    assert Session.objects.filter(runner_binding=b).exists()  # session kept
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_report_bindings.py -v`
Expected: FAIL — `replace_reported_sessions` still writes `EmdashSession`.

- [ ] **Step 3: Rewrite `replace_reported_sessions`**

Replace the body (`apps/harness/services.py:654-712`) with:

```python
@transaction.atomic
def replace_reported_sessions(runner: Runner, workspace, sessions: list) -> int:
    """Upsert a durable Session(origin=runner) + RunnerBinding per reported
    session. Sessions that fell off the report keep their Session row but have
    their live binding cleared."""
    from apps.chat.models import RunnerBinding, Session

    deduped, seen = [], set()
    for s in sessions:
        if s.emdash_task in seen:
            continue
        seen.add(s.emdash_task)
        deduped.append(s)

    now_keys = {s.emdash_task for s in deduped}

    for s in deduped:
        binding = (
            RunnerBinding.objects.select_for_update()
            .filter(runner=runner, session_key=s.emdash_task)
            .first()
        )
        if binding is None:
            session = Session.objects.create(
                workspace=workspace,
                origin=Session.ORIGIN_RUNNER,
                project=s.project or "",
                title=s.emdash_task,
            )
            binding = RunnerBinding(session=session, session_key=s.emdash_task)
        binding.runner = runner
        binding.status = s.status or ""
        binding.last_interacted_at = _aware(s.last_interacted_at)
        binding.live_seen_at = timezone.now()
        binding.tail = list(s.recent_messages or [])
        binding.save()

    # Clear the live pointer on this runner's bindings that were NOT re-reported.
    RunnerBinding.objects.filter(runner=runner).exclude(session_key__in=now_keys).update(
        runner=None
    )

    # Keep the durable SessionLink upsert (runner-reuse path) unchanged.
    for s in deduped:
        if s.project:
            record_session(
                None, f"emdash:{s.emdash_task}", runner=runner, project=s.project,
                workspace=workspace, emdash_task_id=s.emdash_task,
            )

    def _fire_reported():
        from apps.harness.signals import sessions_reported
        sessions_reported.send(sender=Runner, runner=runner)

    transaction.on_commit(_fire_reported)
    return len(deduped)
```

Add `from django.utils import timezone` to the imports if not present (check top of `services.py`).

- [ ] **Step 4: Delete the obsolete `EmdashSession` import**

Remove `from .models import EmdashSession` inside the function (it no longer exists).

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_report_bindings.py -v`
Expected: PASS (all three).

- [ ] **Step 6: Fix/rewrite the old EmdashSession report tests**

In `tests/test_harness_emdash_sessions.py`, replace assertions that query `EmdashSession.objects...` for the report path with the `RunnerBinding`/`Session` equivalents above. Run: `uv run pytest tests/test_harness_emdash_sessions.py -v` — expected PASS.

- [ ] **Step 7: Commit**

```bash
git add apps/harness/services.py tests/test_report_bindings.py tests/test_harness_emdash_sessions.py
git commit -m "feat(harness): report path writes Session+RunnerBinding, not EmdashSession"
```

---

### Task 5: Preserve the `EmdashSessionOut` wire shape from the new model

`list_visible_sessions` and the realtime fan-out must keep emitting the exact same `EmdashSessionOut` JSON so the frontend is untouched. Rewrite them to read `Session` + `RunnerBinding` and map to the frozen schema.

**Files:**
- Modify: `apps/harness/services.py` (`list_visible_sessions`, `:715-736`)
- Modify: `apps/harness/schemas.py` (`EmdashSessionOut`, `:122-138` — repoint resolvers to the binding)
- Modify: `apps/harness/api.py` (`list_sessions`, `:442-446` — return type unchanged)
- Modify: `apps/realtime/signals.py` (`_on_sessions_reported`, `:102-120`)
- Test: `tests/test_harness_emdash_sessions.py` (the list/visibility tests) + `tests/test_realtime_runner_consumer.py`

**Interfaces:**
- Consumes: `RunnerBinding`, `Session`, `Runner.live_status`.
- Produces: `list_visible_sessions(user) -> list[SessionView]` where `SessionView` is a small dataclass/namespace carrying the fields `EmdashSessionOut` reads: `id, emdash_task, project, status, last_interacted_at, recent_messages, workspace_id, runner_name`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_report_bindings.py  (append)
def test_list_visible_sessions_maps_to_wire_shape():
    from apps.harness.services import list_visible_sessions
    from apps.harness.models import Runner
    from django.contrib.auth import get_user_model
    ws = Workspace.objects.create(slug="w1", name="W1")
    user = get_user_model().objects.create(username="jj", email="jj@dimagi.com")
    from apps.workspaces.services import add_member  # existing helper
    add_member(ws, user, role="owner")
    runner = Runner.objects.create(name="laptop", workspace=ws, location=Runner.LOCAL,
                                   status=Runner.ONLINE, paired_by=user)
    replace_reported_sessions(runner, ws, [_reported("feat-x", [{"role": "assistant", "text": "hi"}])])
    rows = list_visible_sessions(user)
    assert len(rows) == 1
    r = rows[0]
    assert r.emdash_task == "feat-x"
    assert r.recent_messages == [{"role": "assistant", "text": "hi"}]
    assert r.runner_name == "laptop"
```

(If `apps.workspaces.services.add_member` differs, use the real membership helper — check `apps/workspaces/services.py`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_report_bindings.py::test_list_visible_sessions_maps_to_wire_shape -v`
Expected: FAIL — `list_visible_sessions` still queries `EmdashSession`.

- [ ] **Step 3: Rewrite `list_visible_sessions`**

Replace `apps/harness/services.py:715-736` with:

```python
from dataclasses import dataclass


@dataclass
class SessionView:
    """The wire projection of a live runner session — the fields EmdashSessionOut
    reads. Derived from Session + RunnerBinding; preserves the frozen shape."""
    id: str
    emdash_task: str
    project: str
    status: str
    last_interacted_at: object
    recent_messages: list
    workspace_id: str
    runner_name: str


def list_visible_sessions(user) -> list[SessionView]:
    from apps.chat.models import RunnerBinding

    wsvc.auto_join_workspaces(user)
    ws_slugs = wsvc.user_workspace_slugs(user)
    bindings = (
        RunnerBinding.objects.filter(
            runner__isnull=False, session__workspace_id__in=ws_slugs
        )
        .select_related("runner", "session")
        .order_by("-last_interacted_at")
    )
    out = []
    for b in bindings:
        if b.runner.live_status != Runner.ONLINE:
            continue
        out.append(SessionView(
            id=str(b.session_id),
            emdash_task=b.session_key,
            project=b.session.project,
            status=b.status,
            last_interacted_at=b.last_interacted_at,
            recent_messages=b.tail,
            workspace_id=b.session.workspace_id,
            runner_name=b.runner.name,
        ))
    return out
```

- [ ] **Step 4: Repoint `EmdashSessionOut` resolvers**

`EmdashSessionOut` reads attributes directly off the object now (the `SessionView` field names match the schema, so `from_orm`/`model_validate` works). Confirm `apps/harness/schemas.py:122-138` fields are: `id, emdash_task, project, status, last_interacted_at, recent_messages, workspace, runner_name`. Update the two custom resolvers to read the `SessionView` fields:

```python
    @staticmethod
    def resolve_workspace(obj) -> str:
        return obj.workspace_id

    @staticmethod
    def resolve_runner_name(obj) -> str:
        return obj.runner_name
```

- [ ] **Step 5: Update the realtime fan-out**

In `apps/realtime/signals.py:102-120`, the `_on_sessions_reported` receiver already calls `list_visible_sessions(runner.paired_by)` and serializes each via `EmdashSessionOut.from_orm(...).model_dump(mode="json")`. Since `list_visible_sessions` now returns `SessionView` with matching field names, this works unchanged — but verify `from_orm` handles the dataclass (Ninja/Pydantic v2 `model_validate(obj, from_attributes=True)`). If `from_orm` chokes on the dataclass, change that line to `EmdashSessionOut.model_validate(v, from_attributes=True)`.

- [ ] **Step 6: Run the wire + realtime tests**

Run: `uv run pytest tests/test_report_bindings.py tests/test_realtime_runner_consumer.py tests/test_harness_emdash_sessions.py -v`
Expected: PASS. Then confirm the frontend types are unchanged:
Run: `cd frontend && npm run gen:api:local` (or against a running backend) — expected: **no diff** to `frontend/src/api/generated.ts` for `EmdashSessionOut`.

- [ ] **Step 7: Commit**

```bash
git add apps/harness/services.py apps/harness/schemas.py apps/harness/api.py apps/realtime/signals.py tests/test_report_bindings.py
git commit -m "feat(harness): serve EmdashSessionOut wire shape from Session+RunnerBinding"
```

---

### Task 6: Rename `apps/chat` → `apps/sessions`

Pure rename, isolated last so the model work above landed first. Uses the standard app-label rename recipe (works with or without data; data-wipe just means `migrate` can run fresh).

**Files:**
- Rename dir: `apps/chat/` → `apps/sessions/`
- Modify: `apps/sessions/apps.py` (`name`/`label`)
- Create: `apps/sessions/migrations/0006_rename_to_sessions.py` (SeparateDatabaseAndState → AlterModelTable for each model)
- Modify: `apps/harness/models.py:195` (`"chat.Session"` → `"sessions.Session"`) + new harness migration
- Modify: `config/settings/base.py:101` (`"apps.chat"` → `"apps.sessions"`)
- Modify: `config/asgi.py:34` (`from apps.chat.routing ...` → `apps.sessions.routing`)
- Modify: `apps/api/api.py:159` (`from apps.chat.api ...` → `apps.sessions.api`)
- Modify: `tests/test_architecture_boundary.py:27` (`"chat"` → `"sessions"` in `FRAMEWORK`)
- Modify: `ARCHITECTURE.md` (tier table: chat → sessions) and `CLAUDE.md` (framework list)
- Modify: every `tests/test_chat_*.py` import (`from apps.chat...` → `from apps.sessions...`), and `test_chat_models.py:23` (`apps.is_installed("apps.chat")` → `"apps.sessions"`)

**Interfaces:**
- Produces: app label `sessions`; model refs `sessions.Session`, etc. `related_name`s and the DB tables may keep or change names — data is disposable, so `AlterModelTable` renaming `chat_*` → `sessions_*` is optional but preferred for tidiness.

- [ ] **Step 1: Move the directory + update `apps.py`**

```bash
git mv apps/chat apps/sessions
```
Edit `apps/sessions/apps.py`:
```python
class SessionsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.sessions"
    label = "sessions"

    def ready(self) -> None:
        from . import signals  # noqa: F401
```

- [ ] **Step 2: Update all references (imports, settings, asgi, api, string FK, tests, docs)**

Run these and fix each hit:
```bash
grep -rln "apps\.chat\|apps/chat\|\"chat\.\|'chat\.\|is_installed(\"apps.chat\")" apps config tests
```
Apply: `config/settings/base.py:101` → `"apps.sessions"`; `config/asgi.py:34` → `apps.sessions.routing`; `apps/api/api.py:159` → `apps.sessions.api`; `apps/harness/models.py:195` → `models.ForeignKey("sessions.Session", ...)`; `tests/test_architecture_boundary.py:27` FRAMEWORK set `"chat"`→`"sessions"`; all `tests/test_chat_*.py` imports; `ARCHITECTURE.md` + `CLAUDE.md`.

- [ ] **Step 3: Create the label-rename migration**

Create `apps/sessions/migrations/0006_rename_to_sessions.py`:
```python
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("sessions", "0005_runnerbinding")]
    # Rename the DB tables from chat_* to sessions_* without touching model
    # state (state already says label=sessions via apps.py).
    operations = [
        migrations.RunSQL("ALTER TABLE chat_session RENAME TO sessions_session;", reverse_sql=migrations.RunSQL.noop),
        migrations.RunSQL("ALTER TABLE chat_message RENAME TO sessions_message;", reverse_sql=migrations.RunSQL.noop),
        migrations.RunSQL("ALTER TABLE chat_sessionparticipant RENAME TO sessions_sessionparticipant;", reverse_sql=migrations.RunSQL.noop),
        migrations.RunSQL("ALTER TABLE chat_draft RENAME TO sessions_draft;", reverse_sql=migrations.RunSQL.noop),
        migrations.RunSQL("ALTER TABLE chat_runnerbinding RENAME TO sessions_runnerbinding;", reverse_sql=migrations.RunSQL.noop),
    ]
```
NOTE: because the data is disposable, the fallback if the label rename fights Django's autodetector is: drop the dev DB and `migrate` fresh (`uv run python manage.py reset_db` if `django_extensions` is available, else drop/create the `canopy_web` database), then this migration's table names already match. Verify with `uv run python manage.py makemigrations --check --dry-run` (expected: no changes).

- [ ] **Step 4: Add the harness FK-repoint migration**

Run: `uv run python manage.py makemigrations harness --name repoint_chat_session_fk`
Expected: an `AlterField` on `Turn.chat_session` now targeting `sessions.Session`. If the autodetector doesn't emit one (string refs resolve at runtime), no migration is needed — confirm with `--check`.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -q`
Expected: PASS. Fix any residual `apps.chat` import misses the grep didn't catch. Then:
Run: `uv run python manage.py makemigrations --check --dry-run`
Expected: "No changes detected".

- [ ] **Step 6: Verify the architecture boundary + frontend build**

Run: `uv run pytest tests/test_architecture_boundary.py -v && cd frontend && npm run build`
Expected: PASS + clean build (frontend still calls `/api/chat` paths — those route strings are unchanged in this plan; only the Django module moved. Confirm `apps/sessions/api.py` still mounts at `/api/chat` in `apps/api/api.py:189` — i.e. leave the URL prefix `"/chat"` for now; renaming the URL is a Plan 4 concern.)

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "refactor: rename apps/chat -> apps/sessions (canonical session system)"
```

---

## Self-Review

**Spec coverage:**
- Unified `Session` + first-class runner → Tasks 1–3 (Runner.location/engine, Session.origin, RunnerBinding). ✅
- `EmdashSession` dropped, absorbed into binding → Task 3 + Task 4. ✅
- Runner-discovered session auto-creates a lightweight `Session(origin=runner)` → Task 4. ✅
- Tier discriminator on the runner (`location`) → Task 1 (the tier RULE — writing full transcripts for cloud — is Plan 2/3, not here). ✅ (persistence behavior deferred by design)
- Wire contract frozen so frontend is untouched → Task 5. ✅
- App rename + boundary test + docs → Task 6. ✅
- **Deferred by Global Constraints (not gaps):** `SessionLink` fold (Plan 3), tail-first loading (Plan 2), attach/detach liveness + runner streaming (Plan 3), frontend surface + URL rename (Plan 4).

**Placeholder scan:** No TBD/TODO; every code step shows real code. The one conditional ("if the autodetector doesn't emit a migration") is a verify-branch with an explicit `--check` command, not a placeholder.

**Type consistency:** `RunnerBinding.session_key` ↔ `SessionView.emdash_task` mapping is explicit in Task 5. `Runner.LOCAL`/`CLOUD`/`ENGINE_EMDASH`, `Session.ORIGIN_WEB`/`ORIGIN_RUNNER` defined in Tasks 1–2 and used consistently. `list_visible_sessions` returns `list[SessionView]` (Task 5) — its only consumers are the two serializers updated in the same task.

## Notes for the implementer

- Verify next migration numbers with `ls apps/harness/migrations/ apps/chat/migrations/` before creating files — the numbers above (`0014`, `0015`, `0004`, `0005`, `0006`) assume current head; adjust if newer migrations exist.
- The `apps.workspaces` membership helper name in Task 5's test is a guess — check `apps/workspaces/services.py` for the real `add_member`/`add_user` signature and use it.
- This plan leaves the URL prefix `/api/chat` and `ws/chat/` untouched (renamed in Plan 4 with the frontend), so the SPA keeps working after each task.
