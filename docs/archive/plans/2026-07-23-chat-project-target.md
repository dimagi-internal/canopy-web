# Chat with a project (not just an agent) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Delete the supervisor `Composer` widget and let the `+ chat` picker start a chat with a **project** (a repo checkout, no persona), listing agents first then projects across all workspaces.

**Architecture:** A `chat.Session` gains a bare `project` string (the emdash project name, mirroring `Turn.project` — no FK, so `apps/chat` stays framework-tier). An agentless session-with-project turn resolves its checkout + workspace from the session at serialization time; the runner is already project-generic, so no runner/emdash change. Frontend: one picker lists agents + projects; the `Composer` is removed.

**Tech Stack:** Django 5 + Django Ninja (Pydantic v2), React 19 + Vite + TypeScript, pytest, vitest, openapi-typescript.

## Global Constraints

- `apps/chat` is framework tier: it MUST NOT import `apps/projects` (product). `Session.project` is a bare `CharField`, never a FK to `projects.Project`. (ARCHITECTURE.md)
- The `harness.Turn` model is unchanged: a turn still targets `agent XOR project XOR chat_session`. A session's project is resolved from `chat_session.project`, NEVER copied onto the `Turn.project` column (the `turn_targets_agent_xor_project_xor_session` CheckConstraint forbids it).
- Django `CheckConstraint` uses `condition=` (this repo is on Django 5.1+; see `apps/harness/models.py`).
- After any `apps/**/schemas.py` or `api.py` change, regenerate `frontend/src/api/generated.ts` (Task 5) — CI's `regen-openapi.yml` fails on stale types.
- Design tokens only in frontend (`bg-card`, `text-muted-foreground`, etc.) — no raw palette literals. (CLAUDE.md)
- Backend tests live in top-level `tests/`. Run with `uv run pytest`.

---

### Task 1: `chat.Session` gains a `project` field + constraint

**Files:**
- Modify: `apps/chat/models.py:46-50` (the `Session.Meta` + add field near line 32)
- Create: `apps/chat/migrations/00NN_session_project.py` (generated)
- Test: `tests/test_chat_models.py`

**Interfaces:**
- Produces: `Session.project: str` (blank default `""`); DB constraint `chat_session_not_agent_and_project` (agent and project never both set).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_chat_models.py`:

```python
import pytest
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction

from apps.chat.models import Session
from apps.workspaces.models import Workspace


@pytest.mark.django_db
def test_session_project_field_and_xor_constraint():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="canopy", display_name="Canopy", created_by=user)

    # project-only session is allowed
    s = Session.objects.create(workspace=ws, created_by=user, project="canopy-web")
    assert s.project == "canopy-web"
    assert s.agent_id is None

    # agentless + projectless still allowed (existing behavior)
    Session.objects.create(workspace=ws, created_by=user)

    # agent + project together is rejected by the DB constraint
    from apps.agents.models import Agent
    agent = Agent.objects.create(slug="echo", name="Echo", workspace=ws, owner=user)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Session.objects.create(workspace=ws, created_by=user, agent=agent, project="canopy-web")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_chat_models.py::test_session_project_field_and_xor_constraint -v`
Expected: FAIL — `TypeError: Session() got unexpected keyword arguments: 'project'` (field doesn't exist yet).

- [ ] **Step 3: Add the field + constraint**

In `apps/chat/models.py`, add the field after the `workspace` FK (after line 32):

```python
    # The repo checkout this session drives (the emdash project name), for an
    # agentless PROJECT chat. A bare string mirroring Turn.project — NOT a FK to
    # projects.Project, so this framework-tier app never imports product code. A
    # session targets an agent XOR a project (or neither).
    project = models.CharField(max_length=100, blank=True, default="")
```

Replace the `Session.Meta` (lines 46-47) with:

```python
    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                # Never both — you chat WITH an agent, or IN a project, not both.
                condition=models.Q(agent__isnull=True) | models.Q(project=""),
                name="chat_session_not_agent_and_project",
            ),
        ]
```

- [ ] **Step 4: Generate the migration**

Run: `uv run python manage.py makemigrations chat`
Expected: creates `apps/chat/migrations/00NN_session_project.py` with `AddField(project)` + `AddConstraint(chat_session_not_agent_and_project)`. Open it and confirm both operations are present.

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run pytest tests/test_chat_models.py::test_session_project_field_and_xor_constraint -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add apps/chat/models.py apps/chat/migrations/ tests/test_chat_models.py
git commit -m "feat(chat): Session.project — a bare repo checkout for project chats"
```

---

### Task 2: chat create API accepts a project

**Files:**
- Modify: `apps/chat/schemas.py:10-13` (SessionCreateIn) and `:30-37` (SessionOut)
- Modify: `apps/chat/services.py:30-42` (create_session)
- Modify: `apps/chat/api.py:27-35` (`_out`) and `:51-66` (create_session)
- Test: `tests/test_chat_api.py`

**Interfaces:**
- Consumes: `Session.project` (Task 1).
- Produces: `POST /api/chat/` accepts `{"project": "<repo>"}`; `SessionOut.project: str`; `services.create_session(..., project="")`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_chat_api.py` (reuse the module's `client`/`ctx` fixtures):

```python
def test_create_project_session(client):
    r = client.post("/api/chat/", data={"project": "canopy-web"}, content_type="application/json")
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["agent_slug"] is None
    assert body["project"] == "canopy-web"


def test_create_rejects_agent_and_project_together(client):
    r = client.post(
        "/api/chat/",
        data={"agent_slug": "echo", "project": "canopy-web"},
        content_type="application/json",
    )
    assert r.status_code == 422, r.content
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_chat_api.py::test_create_project_session tests/test_chat_api.py::test_create_rejects_agent_and_project_together -v`
Expected: FAIL — `project` not accepted / not in response, and both-set not rejected.

- [ ] **Step 3: Add `project` to the schemas**

In `apps/chat/schemas.py`, update `SessionCreateIn` (lines 10-13):

```python
class SessionCreateIn(Schema):
    agent_slug: str | None = None
    # An agentless PROJECT chat: the repo checkout to drive. Mutually exclusive
    # with agent_slug.
    project: str = ""
    title: str = ""
    metadata: dict = {}
```

And `SessionOut` (lines 30-37) — add `project`:

```python
class SessionOut(Schema):
    id: uuid.UUID
    agent_slug: str | None
    project: str
    workspace: str
    title: str
    status: str
    created_at: dt.datetime
```

- [ ] **Step 4: Thread `project` through the service**

In `apps/chat/services.py`, update `create_session` (lines 30-42):

```python
def create_session(*, workspace, created_by, agent=None, project: str = "", title: str = "", metadata: dict | None = None) -> Session:
    # The creator is the owner (SP3 multiplayer). Atomic so a session never exists
    # without its owner participant. Local imports avoid a cycle.
    from .models import SessionParticipant
    from .participants import ensure_participant

    with transaction.atomic():
        session = Session.objects.create(
            workspace=workspace, agent=agent, project=project, created_by=created_by,
            title=title, metadata=metadata or {},
        )
        ensure_participant(session, created_by, SessionParticipant.OWNER)
    return session
```

- [ ] **Step 5: Accept + validate `project` in the API**

In `apps/chat/api.py`, update `_out` (lines 27-35) to surface `project`:

```python
def _out(session: Session) -> dict:
    return {
        "id": session.id,
        "agent_slug": session.agent.slug if session.agent_id else None,
        "project": session.project,
        "workspace": session.workspace_id,
        "title": session.title,
        "status": session.status,
        "created_at": session.created_at,
    }
```

And `create_session` (lines 51-66):

```python
@router.post("/", response=SessionOut, summary="Create a chat session")
def create_session(request: HttpRequest, payload: SessionCreateIn):
    if payload.agent_slug and payload.project:
        raise HttpError(422, "a session targets an agent or a project, not both")
    try:
        workspace = wsvc.current_workspace(request.user, getattr(request, "workspace_slug", None))
    except ValueError as exc:
        raise HttpError(422, str(exc))
    agent = None
    if payload.agent_slug:
        agent = agent_services.get_agent(payload.agent_slug)
        if agent is None or agent.workspace_id != workspace.slug:
            raise HttpError(404, f"agent '{payload.agent_slug}' not found in this workspace")
    session = services.create_session(
        workspace=workspace, created_by=request.user, agent=agent,
        project=payload.project, title=payload.title, metadata=payload.metadata,
    )
    return _out(session)
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_chat_api.py -v`
Expected: PASS (new tests + the existing suite still green).

- [ ] **Step 7: Commit**

```bash
git add apps/chat/schemas.py apps/chat/services.py apps/chat/api.py tests/test_chat_api.py
git commit -m "feat(chat): create a project-targeted chat session"
```

---

### Task 3: harness resolves checkout + workspace from a project session

**Files:**
- Modify: `apps/harness/models.py:288-293` (`Turn.target`)
- Modify: `apps/harness/schemas.py:164-205` (`TurnOut` — add `resolve_project`; update `resolve_workspace_slug`)
- Test: `tests/test_harness_session_turns.py` and `tests/test_chat_serializers.py`

**Interfaces:**
- Consumes: `Session.project` (Task 1).
- Produces: for an agentless session-with-project turn, `Turn.target == chat_session.project`; `TurnOut.project == chat_session.project`; `TurnOut.workspace_slug == chat_session.workspace_id`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_harness_session_turns.py`:

```python
def test_session_turn_target_falls_back_to_project():
    ws, user = _ws_user()
    s = Session.objects.create(workspace=ws, created_by=user, project="canopy-web")
    turn, _ = services.enqueue_turn(
        session=s, origin=Turn.ORIGIN_API, idempotency_key="proj1", prompt="hi"
    )
    assert turn.target == "canopy-web"  # not the session:<hex> marker
```

Add to `tests/test_chat_serializers.py` (mirror its imports — it constructs a Turn and validates `TurnOut`):

```python
def test_turnout_surfaces_project_session_target_and_workspace():
    from django.contrib.auth.models import User
    from apps.chat.models import Session
    from apps.harness import services
    from apps.harness.models import Turn
    from apps.harness.schemas import TurnOut
    from apps.workspaces.models import Workspace

    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="canopy", display_name="Canopy", created_by=user)
    s = Session.objects.create(workspace=ws, created_by=user, project="canopy-web")
    turn, _ = services.enqueue_turn(
        session=s, origin=Turn.ORIGIN_API, idempotency_key="pk", prompt="hi"
    )
    out = TurnOut.model_validate(turn)
    assert out.agent_slug is None
    assert out.project == "canopy-web"
    assert out.workspace_slug == "canopy"
    assert out.target == "canopy-web"
```

(If `tests/test_chat_serializers.py` lacks a `django_db` marker at module level, decorate this test with `@pytest.mark.django_db`.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_harness_session_turns.py::test_session_turn_target_falls_back_to_project tests/test_chat_serializers.py::test_turnout_surfaces_project_session_target_and_workspace -v`
Expected: FAIL — `target` returns `session:<hex>`; `project` is `""`; `workspace_slug` is `None`.

- [ ] **Step 3: Update `Turn.target`**

In `apps/harness/models.py`, replace the session branch of `target` (lines 288-293):

```python
        if self.chat_session_id:
            if self.chat_session.agent_id:
                return self.chat_session.agent.slug
            if self.chat_session.project:
                return self.chat_session.project
            return f"session:{self.chat_session_id.hex[:8]}"
```

- [ ] **Step 4: Update `TurnOut` resolvers**

In `apps/harness/schemas.py`, add a `resolve_project` and update `resolve_workspace_slug` (near lines 191-205):

```python
    @staticmethod
    def resolve_project(obj) -> str:
        # A project turn stores its repo on the column; a PROJECT chat session
        # carries it on the session (the Turn.project column stays empty — the
        # agent XOR project XOR session constraint forbids setting it there).
        if obj.project:
            return obj.project
        cs = getattr(obj, "chat_session", None)
        return cs.project if cs is not None else ""

    @staticmethod
    def resolve_workspace_slug(obj) -> str | None:
        # Agent turns derive tenancy via the agent; project turns store their own;
        # a session turn (agent-backed or project-backed) derives it from the session.
        if obj.agent_id:
            return obj.agent.workspace_id
        cs = getattr(obj, "chat_session", None)
        if cs is not None:
            return cs.workspace_id
        return obj.workspace_id
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_harness_session_turns.py tests/test_chat_serializers.py -v`
Expected: PASS (new tests + existing green).

- [ ] **Step 6: Commit**

```bash
git add apps/harness/models.py apps/harness/schemas.py tests/test_harness_session_turns.py tests/test_chat_serializers.py
git commit -m "feat(harness): resolve checkout + workspace from a project chat session"
```

---

### Task 4: `ProjectSlugOut` carries its workspace

**Files:**
- Modify: `apps/projects/schemas.py:124-129` (ProjectSlugOut)
- Modify: `apps/projects/api.py:297-305` (get_project_slugs)
- Test: `tests/` — add `tests/test_projects_slugs_workspace.py`

**Interfaces:**
- Produces: `GET /api/projects/slugs/` items include `workspace: str` (the workspace slug); still cross-workspace on the flat mount.

- [ ] **Step 1: Write the failing test**

Create `tests/test_projects_slugs_workspace.py`:

```python
import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.projects.models import Project
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


def test_slugs_include_workspace_across_workspaces():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws_a = Workspace.objects.create(slug="alpha", display_name="Alpha", created_by=user)
    ws_b = Workspace.objects.create(slug="beta", display_name="Beta", created_by=user)
    for ws in (ws_a, ws_b):
        WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    Project.objects.create(slug="canopy-web", name="Canopy Web", status="active", workspace=ws_a, created_by=user)
    Project.objects.create(slug="ace-web", name="ACE Web", status="active", workspace=ws_b, created_by=user)

    c = Client()
    c.force_login(user)
    r = c.get("/api/projects/slugs/")
    assert r.status_code == 200, r.content
    by_slug = {p["slug"]: p for p in r.json()}
    # cross-workspace union, each labeled with its own workspace
    assert by_slug["canopy-web"]["workspace"] == "alpha"
    assert by_slug["ace-web"]["workspace"] == "beta"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_projects_slugs_workspace.py -v`
Expected: FAIL — `KeyError: 'workspace'` (field not returned).

- [ ] **Step 3: Add `workspace` to the schema**

In `apps/projects/schemas.py`, update `ProjectSlugOut` (lines 124-129):

```python
class ProjectSlugOut(StrictModel):
    """Slim machine-readable shape from /api/projects/slugs/."""
    slug: str
    name: str
    status: ProjectStatus
    visibility: ProjectVisibility
    workspace: str | None
```

- [ ] **Step 4: Return `workspace` from the endpoint**

In `apps/projects/api.py`, update `get_project_slugs` (lines 299-305). The `.values(...)` key `workspace` maps the FK's `workspace_id` (the slug) automatically:

```python
    projects = (
        _scoped_project_queryset(request)
        .filter(status="active")
        .order_by("slug")
        .values("slug", "name", "status", "visibility", "workspace")
    )
    return [ProjectSlugOut.model_validate(p) for p in projects]
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run pytest tests/test_projects_slugs_workspace.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add apps/projects/schemas.py apps/projects/api.py tests/test_projects_slugs_workspace.py
git commit -m "feat(projects): /slugs/ returns each project's workspace"
```

---

### Task 5: Regenerate the OpenAPI TypeScript types

**Files:**
- Modify: `frontend/src/api/generated.ts` (generated)

**Interfaces:**
- Produces: `components["schemas"]["SessionOut"].project`, `SessionCreateIn.project`, `ProjectSlugOut.workspace`, `TurnOut.project` (already present) available to the frontend.

- [ ] **Step 1: Dump the schema + regenerate**

From the repo root:

```bash
uv run python -c "
import django, json, os
os.environ['DJANGO_SETTINGS_MODULE'] = 'config.settings.test'
django.setup()
from apps.api.api import api
with open('frontend/openapi.json', 'w') as f:
    json.dump(api.get_openapi_schema(), f, indent=2)
print('schema dumped')
"
cd frontend && npm run gen:api:local
```

- [ ] **Step 2: Verify the new fields landed**

Run: `cd frontend && grep -n "project" src/api/generated.ts | grep -iE "SessionOut|SessionCreateIn" ; grep -n "workspace" src/api/generated.ts | grep -i "ProjectSlugOut" -n || true`
Expected: `SessionOut` and `SessionCreateIn` show a `project` member; `ProjectSlugOut` shows `workspace`. (Confirm by opening the `SessionOut` / `ProjectSlugOut` schema blocks.)

- [ ] **Step 3: Type-check compiles**

Run: `cd frontend && npx tsc -b`
Expected: no errors introduced by the regen.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/api/generated.ts frontend/openapi.json
git commit -m "chore: regenerate OpenAPI types (chat project target)"
```

---

### Task 6: Frontend API clients — project slug list + create-with-project

**Files:**
- Modify: `frontend/src/api/projects.ts:9` (add `listSlugs`)
- Modify: `frontend/src/api/chat.ts:55-78` (CreateSessionInput + createSession body)
- Test: none (thin API wrappers; exercised via Task 7's component test + build)

**Interfaces:**
- Consumes: generated `ProjectSlugOut`, `SessionCreateIn` (Task 5).
- Produces: `projectsApi.listSlugs(): Promise<ProjectSlug[]>` where `ProjectSlug = { slug; name; workspace: string | null; ... }`; `createSession({ project })` sends `project`.

- [ ] **Step 1: Add `ProjectSlug` type + `listSlugs`**

In `frontend/src/api/projects.ts`, add the type export near the top (after line 7):

```typescript
export type ProjectSlug = components["schemas"]["ProjectSlugOut"];
```

And add a method inside the `projectsApi` object (after `list`, around line 14):

```typescript
  listSlugs: async (): Promise<ProjectSlug[]> => {
    const { data, error } = await apiV2.GET("/api/projects/slugs/");
    if (error) throw new Error("Failed to load project slugs");
    return Array.from(data) as ProjectSlug[];
  },
```

- [ ] **Step 2: Add `project` to `createSession`**

In `frontend/src/api/chat.ts`, extend `CreateSessionInput` (lines 55-63):

```typescript
export interface CreateSessionInput {
  title?: string;
  agentSlug?: string;
  // Start an agentless PROJECT chat in this repo (the emdash project name).
  // Mutually exclusive with agentSlug.
  project?: string;
  // Create in this workspace (the chosen agent's OR project's) via the tenant
  // route; omit to use the caller's default.
  workspace?: string;
  metadata?: Record<string, unknown>;
}
```

And the body in `createSession` (lines 65-78):

```typescript
export function createSession(
  input: CreateSessionInput = {},
): Promise<ChatSession> {
  const path = input.workspace ? `/api/w/${input.workspace}/chat/` : "/api/chat/";
  return request<ChatSession>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      title: input.title ?? "",
      agent_slug: input.agentSlug ?? null,
      project: input.project ?? "",
      metadata: input.metadata ?? {},
    }),
  });
}
```

- [ ] **Step 3: Type-check compiles**

Run: `cd frontend && npx tsc -b`
Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/api/projects.ts frontend/src/api/chat.ts
git commit -m "feat(frontend): project-slug list + create-chat-with-project clients"
```

---

### Task 7: `+ chat` picker lists agents then projects; starts project chats

**Files:**
- Create: `frontend/src/components/chat/sessionTargetLabel.ts`
- Create: `frontend/src/components/chat/sessionTargetLabel.test.ts`
- Modify: `frontend/src/components/chat/ChatSessionsPanel.tsx`

**Interfaces:**
- Consumes: `projectsApi.listSlugs`, `createSession({ project, workspace })` (Task 6); `ChatSession.project` (Task 5).
- Produces: the picker renders an **Agents** group then a **Projects** group; picking a project calls `createSession({ project, workspace })` and navigates to `/w/<ws>/chat/<id>`; session rows render project sessions by repo name.

- [ ] **Step 1: Write the failing unit test for the row label**

Create `frontend/src/components/chat/sessionTargetLabel.test.ts`:

```typescript
import { describe, expect, it } from 'vitest'
import { sessionTargetLabel } from './sessionTargetLabel'

describe('sessionTargetLabel', () => {
  it('labels an agent session', () => {
    expect(sessionTargetLabel('Echo', '')).toBe('with Echo')
  })
  it('labels a project session by repo name', () => {
    expect(sessionTargetLabel(null, 'canopy-web')).toBe('canopy-web')
  })
  it('labels an agentless, projectless session', () => {
    expect(sessionTargetLabel(null, '')).toBe('no agent')
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/components/chat/sessionTargetLabel.test.ts`
Expected: FAIL — module `./sessionTargetLabel` not found.

- [ ] **Step 3: Write the pure helper**

Create `frontend/src/components/chat/sessionTargetLabel.ts`:

```typescript
// The subtitle for a chat session row: who/what it targets. An agent session
// reads "with <Agent>"; a project session reads the repo name; an agentless,
// projectless session reads "no agent". Kept pure so it is unit-testable.
export function sessionTargetLabel(agentName: string | null, project: string): string {
  if (agentName) return `with ${agentName}`
  if (project) return project
  return 'no agent'
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd frontend && npx vitest run src/components/chat/sessionTargetLabel.test.ts`
Expected: PASS

- [ ] **Step 5: Wire projects into `ChatSessionsPanel`**

In `frontend/src/components/chat/ChatSessionsPanel.tsx`:

(a) Update imports (lines 13-15):

```typescript
import { createSession, listSessions, type ChatSession } from '@/api/chat'
import { listAgents, type AgentOut } from '@/api/agents'
import { projectsApi, type ProjectSlug } from '@/api/projects'
import { relativeTime } from '@/components/activity/turnLog'
import { sessionTargetLabel } from './sessionTargetLabel'
```

(b) Add project state next to the agents state (after line 33):

```typescript
  const [projects, setProjects] = useState<ProjectSlug[]>([])
```

(c) Load projects alongside sessions/agents. Replace the effect body (lines 42-60) so the `jobs` array also fetches slugs and stores them:

```typescript
  useEffect(() => {
    let live = true
    setLoading(true)
    const jobs: Promise<unknown>[] = [listSessions(), projectsApi.listSlugs()]
    if (!agentsProp) jobs.push(listAgents({ limit: 100 }))
    Promise.allSettled(jobs).then((results) => {
      if (!live) return
      const s = results[0]
      if (s.status === 'fulfilled') setSessions(s.value as ChatSession[])
      else setError(s.reason instanceof Error ? s.reason.message : 'failed to load sessions')
      if (results[1]?.status === 'fulfilled') setProjects(results[1].value as ProjectSlug[])
      if (!agentsProp && results[2]?.status === 'fulfilled') {
        setAgents((results[2].value as { items: AgentOut[] }).items)
      }
      setLoading(false)
    })
    return () => {
      live = false
    }
  }, [agentsProp])
```

(d) Add a `startProjectChat` callback next to `startChat` (after line 78):

```typescript
  const startProjectChat = useCallback(
    (project: ProjectSlug) => {
      setCreating(true)
      createSession({ project: project.slug, workspace: project.workspace ?? undefined })
        .then((s) => navigate(`/w/${s.workspace}/chat/${s.id}`))
        .catch((err: unknown) => {
          setError(err instanceof Error ? err.message : 'could not start chat')
          setCreating(false)
        })
    },
    [navigate],
  )
```

(e) Update the trigger's disabled guard (line 87) so the button stays enabled when only projects exist:

```typescript
          <DropdownMenuTrigger render={<Button size="sm" disabled={creating || (agents.length === 0 && projects.length === 0)} />}>
```

(f) Extend the dropdown content — after the agents `.map(...)` and the existing `<DropdownMenuSeparator />` (lines 94-100), add a Projects group:

```typescript
            {agents.map((a) => (
              <DropdownMenuItem key={`${a.workspace}/${a.slug}`} onClick={() => startChat(a)}>
                {a.name}
                {a.workspace ? <span className="ml-2 text-xs text-muted-foreground">{a.workspace}</span> : null}
              </DropdownMenuItem>
            ))}
            {projects.length > 0 && (
              <>
                <DropdownMenuSeparator />
                <DropdownMenuLabel>Projects</DropdownMenuLabel>
                {projects.map((p) => (
                  <DropdownMenuItem key={`${p.workspace}/${p.slug}`} onClick={() => startProjectChat(p)}>
                    {p.name}
                    {p.workspace ? <span className="ml-2 text-xs text-muted-foreground">{p.workspace}</span> : null}
                  </DropdownMenuItem>
                ))}
              </>
            )}
```

(Remove the now-redundant trailing `<DropdownMenuSeparator />` at old line 100.)

(g) Render project sessions by repo name — replace the subtitle line (lines 116, 127-130). Change `const who = agentName(s.agent_slug)` block to use the helper:

```typescript
          {sessions.map((s) => {
            const label = sessionTargetLabel(agentName(s.agent_slug), s.project ?? '')
            return (
              <li key={s.id}>
                <Link
                  to={`/w/${s.workspace}/chat/${s.id}`}
                  className="flex items-center justify-between gap-3 px-3 py-2.5 hover:bg-muted"
                >
                  <div className="min-w-0">
                    <div className="truncate text-sm font-medium text-foreground">
                      {s.title?.trim() || 'Untitled chat'}
                    </div>
                    <div className="truncate text-xs text-muted-foreground">
                      {label} · {s.workspace}
                      {s.status !== 'active' ? ` · ${s.status}` : ''}
                    </div>
                  </div>
                  <div className="shrink-0 text-xs text-muted-foreground">
                    {relativeTime(s.created_at, now)}
                  </div>
                </Link>
              </li>
            )
          })}
```

- [ ] **Step 6: Type-check + full frontend unit tests pass**

Run: `cd frontend && npx tsc -b && npx vitest run src/components/chat/`
Expected: PASS, no type errors.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/components/chat/ChatSessionsPanel.tsx frontend/src/components/chat/sessionTargetLabel.ts frontend/src/components/chat/sessionTargetLabel.test.ts
git commit -m "feat(frontend): + chat picker lists projects; project chat sessions"
```

---

### Task 8: Remove the `Composer` widget + dead code

**Files:**
- Modify: `frontend/src/pages/SupervisorPage.tsx:11,198`
- Delete: `frontend/src/components/supervisor/Composer.tsx`
- Delete: `frontend/src/lib/dispatchPrompt.ts`, `frontend/src/lib/dispatchPrompt.test.ts`

**Interfaces:**
- Consumes: nothing new. `enqueueTurn` in `frontend/src/api/harness.ts` is RETAINED (used elsewhere — do not touch).

- [ ] **Step 1: Confirm no other importers**

Run: `cd frontend && grep -rn "Composer\|dispatchPrompt" src`
Expected: only `src/pages/SupervisorPage.tsx`, `src/components/supervisor/Composer.tsx`, `src/lib/dispatchPrompt.ts`, `src/lib/dispatchPrompt.test.ts`. If anything else appears, stop and reassess.

- [ ] **Step 2: Remove the import + usage in `SupervisorPage`**

In `frontend/src/pages/SupervisorPage.tsx`, delete the import (line 11):

```typescript
import { Composer } from '@/components/supervisor/Composer'
```

And in the Sessions tab (line 198), delete the Composer render so the block reads:

```tsx
        <TabsContent value="sessions" className="flex flex-col gap-4">
          <ChatSessionsPanel agents={agents ?? undefined} heading="Chats" />
        </TabsContent>
```

- [ ] **Step 3: Delete the dead files**

```bash
cd frontend && git rm src/components/supervisor/Composer.tsx src/lib/dispatchPrompt.ts src/lib/dispatchPrompt.test.ts
```

- [ ] **Step 4: Type-check + build + tests pass**

Run: `cd frontend && npx tsc -b && npm run build && npx vitest run`
Expected: build succeeds; no references to the deleted modules; all unit tests pass.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/SupervisorPage.tsx
git commit -m "feat(supervisor): remove the Composer widget (folded into + chat)"
```

---

### Task 9: Full verification

- [ ] **Step 1: Backend suite**

Run: `uv run pytest`
Expected: PASS (all green, including the new tests).

- [ ] **Step 2: Frontend build + unit tests**

Run: `cd frontend && npm run build && npx vitest run`
Expected: PASS.

- [ ] **Step 3: Manual render check (per "verify frontend render, not curl")**

Boot the app (`uv run honcho start -f Procfile.dev`), open `/supervisor` → **Sessions** tab:
- The `Composer` box is gone.
- `+ chat` shows **agents first**, a separator, then a **Projects** group (projects from all your workspaces, each with its workspace chip).
- Starting a chat from a project opens `/w/<project-workspace>/chat/<id>`; sending a message projects a reply (stub in dev).
- The session list shows a project chat labeled by its repo name.

- [ ] **Step 4: Final commit (if any manual-fix tweaks were needed)**

```bash
git add -A && git commit -m "test: verify chat-with-project end to end"
```

---

## Self-Review

**Spec coverage:**
- Delete Composer → Task 8. ✓
- `+ chat` lists agents then projects (cross-workspace union) → Tasks 4, 6, 7. ✓
- Project chat runs in the repo checkout via bare `Session.project` → Tasks 1, 3. ✓ (runner unchanged — confirmed in spec.)
- create API accepts project, agent XOR project, `SessionOut.project` → Task 2. ✓
- `Turn.target` + `TurnOut` resolve checkout + workspace from the session → Task 3. ✓
- `ProjectSlugOut.workspace` → Task 4. ✓
- Regenerate OpenAPI types → Task 5. ✓
- Session-list rows render project sessions by repo name → Task 7. ✓
- Tradeoff (Composer one-shot goes away) → accepted, no task needed. ✓
- Known limitation (project session claimable by any session-capable runner) → out of scope, documented in spec. ✓

**Placeholder scan:** none — every code/step is concrete.

**Type consistency:** `Session.project: str` (Task 1) ↔ `SessionCreateIn.project` / `SessionOut.project` (Task 2) ↔ generated `ChatSession.project` (Task 5) ↔ `s.project` (Task 7). `ProjectSlugOut.workspace` (Task 4) ↔ `ProjectSlug.workspace` ↔ `project.workspace` (Tasks 6, 7). `projectsApi.listSlugs` (Task 6) ↔ used in Task 7. `createSession({ project, workspace })` (Task 6) ↔ called in Task 7. `sessionTargetLabel(agentName, project)` defined + used in Task 7. Consistent.
