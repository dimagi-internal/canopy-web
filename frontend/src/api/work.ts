// The workspace's work: tasks by key, the workspace task list, Triage, labels,
// saved views, task settings, and a project by its address with its milestones
// and updates. A thin typed wrapper over the generated client, like agents.ts;
// apiV2 pins every call to the page's workspace (`/api/w/{ws}/…`).
import { apiV2, WORKSPACE_HEADER } from './client.v2'
import type { components, operations } from './generated'
import { ensureOk, unwrap } from './problem'

type Schemas = components['schemas']

export type Task = Schemas['AgentTaskOut']
export type TaskDetail = Schemas['TaskDetailOut']
export type TaskEvent = Schemas['TaskEventOut']
export type TaskRelation = Schemas['TaskRelationOut']
export type TaskBrief = Schemas['TaskBriefOut']
export type TaskPatch = Schemas['AgentTaskPatch']
export type TaskCreate = Schemas['TaskCreateIn']
export type TaskActionName = Schemas['AgentTaskActionIn']['action']
export type TaskStatus = Task['status']
export type ActResult = Schemas['ActOut']
export type BulkResult = Schemas['BulkTaskResultOut']
export type TriageEntry = Schemas['TriageOut']
export type Label = Schemas['LabelOut']
export type SavedView = Schemas['SavedViewOut']
export type SavedViewIn = Schemas['SavedViewIn']
export type SavedViewPatch = Schemas['SavedViewPatch']
export type TaskSettings = Schemas['TaskSettingsOut']
export type Project = Schemas['AgentProjectOut']
export type ProjectDetail = Schemas['AgentProjectDetailOut']
export type ProjectPatch = Schemas['AgentProjectPatch']
export type ProjectCreate = Schemas['WorkspaceProjectIn']
export type Milestone = Schemas['MilestoneOut']
export type ProjectUpdate = Schemas['ProjectUpdateOut']
export type Health = 'on_track' | 'at_risk' | 'off_track'

/** The task list's query: every filter `GET /api/tasks/` takes. */
export type TaskQuery = NonNullable<operations['list_fleet_tasks']['parameters']['query']>

// The generated shapes are readonly (--immutable), and openapi-fetch's Readable<T>
// degrades their nested readonly arrays to ArrayLike (see agents.ts `toPage`), so
// a response is not assignable to its own alias. The JSON is the alias's shape;
// bridge the type at this one boundary.
function typed<T>(value: unknown): T {
  return value as T
}

function pin(workspace?: string) {
  return workspace ? { headers: { [WORKSPACE_HEADER]: workspace } } : {}
}

function clean<T extends Record<string, unknown>>(q: T): T {
  return Object.fromEntries(Object.entries(q).filter(([, v]) => v !== '' && v !== undefined && v !== null)) as T
}

// ---- tasks -----------------------------------------------------------------

export async function listTasks(query: TaskQuery = {}, workspace?: string): Promise<Task[]> {
  const res = await apiV2.GET('/api/tasks/', { params: { query: clean(query) }, ...pin(workspace) })
  return typed(Array.from(unwrap(res, 'listTasks')))
}

export async function countTasks(
  query: NonNullable<operations['count_tasks']['parameters']['query']> = {},
  workspace?: string,
): Promise<number> {
  const res = await apiV2.GET('/api/tasks/count/', { params: { query: clean(query) }, ...pin(workspace) })
  return unwrap(res, 'countTasks').count
}

export async function getTask(key: string): Promise<TaskDetail> {
  const res = await apiV2.GET('/api/tasks/{key}/', { params: { path: { key } } })
  return typed(unwrap(res, 'getTask'))
}

export async function createTask(body: Partial<TaskCreate> & { title: string }, workspace?: string): Promise<Task> {
  const res = await apiV2.POST('/api/tasks/', { body: body as TaskCreate, ...pin(workspace) })
  return typed(unwrap(res, 'createTask'))
}

export async function updateTask(key: string, body: TaskPatch): Promise<Task> {
  const res = await apiV2.PATCH('/api/tasks/{key}/', { params: { path: { key } }, body })
  return typed(unwrap(res, 'updateTask'))
}

/** `workspace` pins the call where the page names none (Home, the Supervisor). */
export async function actOn(key: string, action: TaskActionName, comment = '', workspace?: string): Promise<ActResult> {
  const res = await apiV2.POST('/api/tasks/{key}/actions', {
    params: { path: { key } },
    body: { action, comment },
    ...pin(workspace),
  })
  return typed(unwrap(res, 'actOn'))
}

export async function bulkTasks(
  keys: string[],
  change: { patch?: TaskPatch; action?: { action: TaskActionName; comment?: string } },
): Promise<BulkResult[]> {
  const res = await apiV2.POST('/api/tasks/bulk', {
    body: {
      keys,
      patch: change.patch ?? null,
      action: change.action ? { action: change.action.action, comment: change.action.comment ?? '' } : null,
    },
  })
  return typed(Array.from(unwrap(res, 'bulkTasks')))
}

export async function addRelation(
  key: string,
  kind: 'blocks' | 'blocked_by' | 'related' | 'duplicate',
  other: string,
): Promise<TaskDetail> {
  const res = await apiV2.POST('/api/tasks/{key}/relations/', { params: { path: { key } }, body: { kind, other } })
  return typed(unwrap(res, 'addRelation'))
}

export async function removeRelation(key: string, kind: string, other: string): Promise<TaskDetail> {
  const res = await apiV2.DELETE('/api/tasks/{key}/relations/{kind}/{other}', {
    params: { path: { key, kind, other } },
  })
  return typed(unwrap(res, 'removeRelation'))
}

export async function listTriage(query: { project?: string } = {}, workspace?: string): Promise<TriageEntry[]> {
  const res = await apiV2.GET('/api/triage/', { params: { query: clean(query) }, ...pin(workspace) })
  return typed(Array.from(unwrap(res, 'listTriage')))
}

// ---- labels, views, settings ------------------------------------------------

export async function listLabels(workspace?: string): Promise<Label[]> {
  const res = await apiV2.GET('/api/labels/', pin(workspace))
  return typed(Array.from(unwrap(res, 'listLabels')))
}

export async function createLabel(workspace: string, name: string, color = 'muted'): Promise<Label> {
  const res = await apiV2.POST('/api/labels/', { body: { workspace, name, color, description: '' } })
  return typed(unwrap(res, 'createLabel'))
}

export async function updateLabel(id: number, body: Schemas['LabelPatch']): Promise<Label> {
  const res = await apiV2.PATCH('/api/labels/{label_id}', { params: { path: { label_id: id } }, body })
  return typed(unwrap(res, 'updateLabel'))
}

export async function deleteLabel(id: number): Promise<void> {
  ensureOk(await apiV2.DELETE('/api/labels/{label_id}', { params: { path: { label_id: id } } }), 'deleteLabel')
}

export async function listViews(workspace?: string): Promise<SavedView[]> {
  const res = await apiV2.GET('/api/views/', pin(workspace))
  return typed(Array.from(unwrap(res, 'listViews')))
}

export async function createView(body: SavedViewIn): Promise<SavedView> {
  const res = await apiV2.POST('/api/views/', { body })
  return typed(unwrap(res, 'createView'))
}

export async function updateView(id: number, body: SavedViewPatch): Promise<SavedView> {
  const res = await apiV2.PATCH('/api/views/{view_id}', { params: { path: { view_id: id } }, body })
  return typed(unwrap(res, 'updateView'))
}

export async function deleteView(id: number): Promise<void> {
  ensureOk(await apiV2.DELETE('/api/views/{view_id}', { params: { path: { view_id: id } } }), 'deleteView')
}

export async function getTaskSettings(workspace: string): Promise<TaskSettings> {
  const res = await apiV2.GET('/api/task-settings/', { params: { query: { workspace } } })
  return typed(unwrap(res, 'getTaskSettings'))
}

export async function setTaskSettings(workspace: string, body: Schemas['TaskSettingsPatch']): Promise<TaskSettings> {
  const res = await apiV2.PATCH('/api/task-settings/', { params: { query: { workspace } }, body })
  return typed(unwrap(res, 'setTaskSettings'))
}

// ---- projects ---------------------------------------------------------------

export async function listProjects(status = 'open', workspace?: string): Promise<Project[]> {
  const res = await apiV2.GET('/api/projects/', { params: { query: { status } }, ...pin(workspace) })
  return typed(Array.from(unwrap(res, 'listProjects')))
}

export async function getProjectByAddress(workspace: string, handle: string): Promise<ProjectDetail> {
  const res = await apiV2.GET('/api/workspace-projects/{workspace}/{handle}/', {
    params: { path: { workspace, handle } },
  })
  return typed(unwrap(res, 'getProjectByAddress'))
}

export async function createWorkspaceProject(body: Partial<ProjectCreate> & { name: string }): Promise<Project> {
  const res = await apiV2.POST('/api/workspace-projects/', { body: body as ProjectCreate })
  return typed(unwrap(res, 'createWorkspaceProject'))
}

export async function updateProject(workspace: string, handle: string, body: ProjectPatch): Promise<Project> {
  const res = await apiV2.PATCH('/api/workspace-projects/{workspace}/{handle}/', {
    params: { path: { workspace, handle } },
    body,
  })
  return typed(unwrap(res, 'updateProject'))
}

export async function listMilestones(workspace: string, handle: string): Promise<Milestone[]> {
  const res = await apiV2.GET('/api/workspace-projects/{workspace}/{handle}/milestones/', {
    params: { path: { workspace, handle } },
  })
  return typed(Array.from(unwrap(res, 'listMilestones')))
}

export async function createMilestone(
  workspace: string,
  handle: string,
  body: { name: string; description?: string; target_date?: string | null },
): Promise<Milestone> {
  const res = await apiV2.POST('/api/workspace-projects/{workspace}/{handle}/milestones/', {
    params: { path: { workspace, handle } },
    body: { description: '', target_date: null, ...body },
  })
  return typed(unwrap(res, 'createMilestone'))
}

export async function updateMilestone(
  workspace: string,
  handle: string,
  id: number,
  body: Schemas['MilestonePatch'],
): Promise<Milestone> {
  const res = await apiV2.PATCH('/api/workspace-projects/{workspace}/{handle}/milestones/{milestone_id}', {
    params: { path: { workspace, handle, milestone_id: id } },
    body,
  })
  return typed(unwrap(res, 'updateMilestone'))
}

export async function deleteMilestone(workspace: string, handle: string, id: number): Promise<void> {
  ensureOk(
    await apiV2.DELETE('/api/workspace-projects/{workspace}/{handle}/milestones/{milestone_id}', {
      params: { path: { workspace, handle, milestone_id: id } },
    }),
    'deleteMilestone',
  )
}

export async function listUpdates(workspace: string, handle: string): Promise<ProjectUpdate[]> {
  const res = await apiV2.GET('/api/workspace-projects/{workspace}/{handle}/updates/', {
    params: { path: { workspace, handle } },
  })
  return typed(Array.from(unwrap(res, 'listUpdates')))
}

export async function postUpdate(
  workspace: string,
  handle: string,
  body: { health: Health; body: string; draft?: boolean },
): Promise<ProjectUpdate> {
  const res = await apiV2.POST('/api/workspace-projects/{workspace}/{handle}/updates/', {
    params: { path: { workspace, handle } },
    body: { draft: false, checkin_run: null, ...body },
  })
  return typed(unwrap(res, 'postUpdate'))
}

export async function publishUpdate(
  workspace: string,
  handle: string,
  id: number,
  body: { health?: Health; body?: string },
): Promise<ProjectUpdate> {
  const res = await apiV2.POST('/api/workspace-projects/{workspace}/{handle}/updates/{update_id}/publish', {
    params: { path: { workspace, handle, update_id: id } },
    body: { health: body.health ?? null, body: body.body ?? null },
  })
  return typed(unwrap(res, 'publishUpdate'))
}
