// Agent Workspace API client — a thin, typed wrapper over the generated
// OpenAPI client. Response entity types alias the generated schemas, so this
// file cannot drift from the server. Workspace scoping is handled by apiV2's
// middleware (see WS_SCOPED_API_PREFIXES in ./client.v2), not here.
import { apiV2, WORKSPACE_HEADER } from './client.v2'
import type { components } from './generated'
import { ApiError, ensureOk, unwrap } from './problem'
import type { Cli } from '@/lib/clis'

type Schemas = components['schemas']

export type AgentOut = Schemas['AgentOut']
export type AgentDetailOut = Schemas['AgentDetailOut']
export type AgentTurnOut = Schemas['AgentTurnOut']
export type AgentSyncOut = Schemas['AgentSyncOut']
export type AgentSkillOut = Schemas['AgentSkillOut']
export type AgentTaskLink = Schemas['AgentTaskLink']
export type TaskOut = Schemas['AgentTaskOut']
export type TaskDetailOut = Schemas['AgentTaskDetailOut']
export type TaskActionOut = Schemas['AgentTaskActionOut']
export type TaskAction = Schemas['AgentTaskActionIn']['action']
export type TaskStatus = TaskOut['status']
export type ProjectOut = Schemas['AgentProjectOut']
export type ProjectDetailOut = Schemas['AgentProjectDetailOut']
export type ProjectIdeasOut = Schemas['ProjectIdeasOut']
export type IdeaOut = Schemas['IdeaOut']
export type IdeaWhoOut = Schemas['IdeaWhoOut']
export type IdeaCreatedOut = Schemas['IdeaCreatedOut']
export type ProjectTeamOut = Schemas['ProjectTeamOut']
export type WaitingLineOut = Schemas['WaitingLineOut']
export type ContributorBriefOut = Schemas['ContributorBriefOut']
export type ContributorPageOut = Schemas['ContributorPageOut']
export type AgentRunnerOut = Schemas['AgentRunnerOut']
export type AgentPinnableRunnerOut = Schemas['AgentPinnableRunnerOut']
export type AgentRoutePreviewOut = Schemas['AgentRoutePreviewOut']
export type AgentRunnerRuleOut = Schemas['AgentRunnerRuleOut']
// The routable source union, straight off the generated request schema — the
// picker and the rule rows both key on it, so there is no hand-kept copy.
export type RoutableSource = Schemas['AgentRunnerRuleIn']['source']
// A rule's mode: '' (defer to the row below), 'manual' or 'auto'.
export type RuleTurnMode = NonNullable<Schemas['AgentRunnerRuleIn']['turn_mode']>

// The two runtime autonomy postures, straight off the request schema so the
// toggle can't drift from the server's accepted values.
export type TurnMode = Schemas['TurnModeIn']['turn_mode']


// Stays hand-declared, deliberately: openapi-typescript emits a CONCRETE alias
// per payload (Page_AgentOut_, Page_AgentSyncOut_, …), never a generic, so
// there is nothing to alias a generic to. Mutable because callers assign
// page.items straight into useState.
export interface Page<T> {
  items: T[]
  total: number
  offset: number
  limit: number
}

export interface ListAgentsParams {
  limit?: number
}

// openapi-fetch returns { data, error }; `unwrap` (./problem) returns the data
// or throws an `ApiError` carrying the HTTP status and the server's `detail`. A
// 401 never reaches here: apiV2's middleware redirects to login first. A few
// failures ARE user-facing on a task card: acting on an already-closed ask (409
// — the card refetches) and an empty reply (422).
//
// `AgentApiError` is the shared `ApiError` under its old name, so existing
// `instanceof AgentApiError` checks keep working.
export { ApiError as AgentApiError }

// Generated shapes are readonly (--immutable); Page<T> is mutable. Copy across
// the boundary rather than casting, so the compiler keeps checking us.
//
// openapi-fetch's Readable<T> helper (which strips writeOnly fields from every
// response) recurses into object types with a mapped type that doesn't
// preserve `readonly Foo[]` as an array — it degrades to an ArrayLike-shaped
// object (numeric index + length, no Symbol.iterator). That's a real gap in
// openapi-fetch 0.17 + openapi-typescript-helpers 0.1 against `--immutable`
// codegen, not a hand-wave: `[...res.data.items]` fails to compile (TS2488,
// missing `[Symbol.iterator]`) while `Array.from(res.data.items)` succeeds,
// because Array.from's ArrayLike overload only needs `length` + a numeric
// index signature, which the degraded shape still has. So accept ArrayLike<T>
// here (structural, no cast) and convert with Array.from.
function toPage<T>(p: {
  readonly items: ArrayLike<T>
  readonly total: number
  readonly offset: number
  readonly limit: number
}): Page<T> {
  return {
    items: Array.from(p.items),
    total: p.total,
    offset: p.offset,
    limit: p.limit,
  }
}

export async function listAgents(params: ListAgentsParams = {}): Promise<Page<AgentOut>> {
  const res = await apiV2.GET('/api/agents/', { params: { query: { limit: params.limit } } })
  return agentPage(toPage(unwrap(res, 'listAgents')))
}

/** Every agent you can see in every workspace you are in — a user-level view,
 *  for pages off any one workspace (Supervisor, the IDE, the chat home). */
export async function listMyAgents(params: ListAgentsParams = {}): Promise<Page<AgentOut>> {
  const res = await apiV2.GET('/api/me/agents/', { params: { query: { limit: params.limit } } })
  return agentPage(toPage(unwrap(res, 'listMyAgents')))
}

function agentPage<A extends Omit<AgentOut, 'runner_preference' | 'clis'> & {
  runner_preference?: ArrayLike<string> | null
  clis?: ArrayLike<string> | null
}>(
  page: Page<A>,
): Page<AgentOut> {
  // runner_preference and clis degrade the same way one level down (see toPage's
  // comment); rebuild each item.
  return {
    ...page,
    items: page.items.map((a) => ({
      ...a,
      runner_preference: a.runner_preference ? Array.from(a.runner_preference) : undefined,
      clis: Array.from(a.clis ?? ['claude-code']),
    })),
  }
}

// AgentDetailOut carries a readonly-array field (runner_preference) that
// openapi-fetch's Readable<T> degrades to an ArrayLike, breaking type identity
// (same quirk toPage documents). Rebuild it to a real array at the boundary.
function normalizeAgentDetail(data: { runner_preference?: ArrayLike<string> }): AgentDetailOut {
  // Spread carries every field at runtime; TS only tracks runner_preference here,
  // so bridge through unknown (the degraded ArrayLike doesn't overlap the alias).
  return { ...data, runner_preference: Array.from(data.runner_preference ?? []) } as unknown as AgentDetailOut
}

export async function getAgent(slug: string): Promise<AgentDetailOut> {
  const res = await apiV2.GET('/api/agents/{slug}/', { params: { path: { slug } } })
  return normalizeAgentDetail(unwrap(res, 'getAgent'))
}

/** Set the agent's Google account ("" = the same as its mailbox, if that is Google).
 *  Through the ordinary upsert, which rewrites name/description/persona/email/avatar,
 *  so those are sent back exactly as they are. Returns the stored value. */
export async function setAgentGoogleAccount(agent: AgentDetailOut, googleAccount: string): Promise<string> {
  const res = await apiV2.POST('/api/agents/', {
    body: {
      slug: agent.slug, name: agent.name, description: agent.description, persona: agent.persona,
      email: agent.email, avatar_url: agent.avatar_url, google_account: googleAccount,
      // "" keeps it where it is; naming another workspace would MOVE it.
      workspace: '',
    },
  })
  const out = unwrap(res, 'setAgentGoogleAccount')
  return out.google_account ?? ''
}

export async function listAgentSyncs(
  slug: string,
  params: ListAgentsParams = {},
): Promise<Page<AgentSyncOut>> {
  const res = await apiV2.GET('/api/agents/{slug}/syncs/', {
    params: { path: { slug }, query: { limit: params.limit } },
  })
  return toPage(unwrap(res, 'listAgentSyncs'))
}

export async function listAgentTurns(
  slug: string,
  params: ListAgentsParams = {},
): Promise<Page<AgentTurnOut>> {
  const res = await apiV2.GET('/api/agents/{slug}/turns/', {
    params: { path: { slug }, query: { limit: params.limit } },
  })
  const page = toPage(unwrap(res, 'listAgentTurns'))
  // AgentTurnOut's own array fields degrade the same way one level down
  // (see toPage's comment) — rebuild each item.
  return {
    ...page,
    items: page.items.map((t) => ({
      ...t,
      task_ext_ids: t.task_ext_ids ? Array.from(t.task_ext_ids) : undefined,
      work_product_urls: t.work_product_urls ? Array.from(t.work_product_urls) : undefined,
    })),
  }
}

export async function listAgentSkills(slug: string): Promise<AgentSkillOut[]> {
  const res = await apiV2.GET('/api/agents/{slug}/skills/', { params: { path: { slug } } })
  return Array.from(unwrap(res, 'listAgentSkills'))
}

export type SkillHistoryOut = Schemas['SkillHistoryOut']

// SkillHistoryOut nests several readonly arrays (groups, present, commits,
// skills, and skills[].revisions itself an array of arrays) — each one
// degrades under openapi-fetch's Readable<T> the same way toPage's comment
// above describes for a single array field. Rebuilding every level by hand
// isn't worth it for a read-only payload the model consumes structurally, so
// bridge through unknown at the boundary like github.ts/schedules.ts/
// workspaces.ts already do for the same reason.
export async function getSkillHistory(slug: string): Promise<SkillHistoryOut> {
  const res = await apiV2.GET('/api/agents/{slug}/skill-history/', { params: { path: { slug } } })
  return unwrap(res, 'getSkillHistory') as unknown as SkillHistoryOut
}

export async function syncSkillHistory(slug: string): Promise<SkillHistoryOut> {
  const res = await apiV2.POST('/api/agents/{slug}/skill-history/sync', { params: { path: { slug } } })
  return unwrap(res, 'syncSkillHistory') as unknown as SkillHistoryOut
}

export interface TaskFilters {
  project?: string
  status?: string
  waiting?: 'me'
  ask?: 'open' | 'closed'
  batch?: string
}

// Drop unset filters so they never reach the query string as `key=undefined`.
function definedOnly<T extends object>(f: T): Partial<T> {
  return Object.fromEntries(
    Object.entries(f).filter(([, v]) => v !== undefined && v !== ''),
  ) as Partial<T>
}

// A task is addressed by (agent slug, ext_id); `ref` is the ext_id.
export async function listTasks(slug: string, f: TaskFilters = {}): Promise<TaskOut[]> {
  const res = await apiV2.GET('/api/agents/{slug}/tasks/', {
    params: { path: { slug }, query: definedOnly(f) },
  })
  return Array.from(unwrap(res, 'listTasks')) as TaskOut[]
}

// Every task the caller can see across the fleet; `agent` narrows to one slug.
/** Tasks across every workspace you are in — a user-level view (`waiting: 'me'`
 *  is your inbox everywhere), for pages off any one workspace. */
export async function listMyTasks(
  f: Pick<TaskFilters, 'project' | 'status' | 'waiting' | 'ask'> & { agent?: string } = {},
): Promise<TaskOut[]> {
  const res = await apiV2.GET('/api/me/tasks/', { params: { query: definedOnly(f) } })
  return Array.from(unwrap(res, 'listMyTasks')) as TaskOut[]
}

export async function listFleetTasks(f: TaskFilters & { agent?: string } = {}): Promise<TaskOut[]> {
  const res = await apiV2.GET('/api/tasks/', { params: { query: definedOnly(f) } })
  return Array.from(unwrap(res, 'listFleetTasks')) as TaskOut[]
}

export async function getTask(slug: string, ref: string): Promise<TaskDetailOut> {
  const res = await apiV2.GET('/api/agents/{slug}/tasks/{ref}/', {
    params: { path: { slug, ref } },
  })
  return unwrap(res, 'getTask') as TaskDetailOut
}

export type ActResult = Schemas['ActOut']

export async function actOnTask(
  slug: string,
  ref: string,
  action: TaskAction,
  comment?: string,
  /** Pin the agent's workspace, for a page that is not under it (Home). */
  workspace?: string,
): Promise<ActResult> {
  const res = await apiV2.POST('/api/agents/{slug}/tasks/{ref}/actions', {
    params: { path: { slug, ref } },
    ...(workspace ? { headers: { [WORKSPACE_HEADER]: workspace } } : {}),
    body: { action, comment: comment ?? '' },
  })
  return unwrap(res, 'actOnTask') as ActResult
}

export async function patchTask(
  slug: string,
  ref: string,
  body: Schemas['AgentTaskPatch'],
): Promise<TaskOut> {
  const res = await apiV2.PATCH('/api/agents/{slug}/tasks/{ref}/', {
    params: { path: { slug, ref } },
    body,
  })
  return unwrap(res, 'patchTask') as TaskOut
}

export async function listProjects(slug: string, status?: string): Promise<ProjectOut[]> {
  const res = await apiV2.GET('/api/agents/{slug}/projects/', {
    params: { path: { slug }, query: status ? { status } : {} },
  })
  return Array.from(unwrap(res, 'listProjects')) as ProjectOut[]
}

export async function getProject(slug: string, ref: string): Promise<ProjectDetailOut> {
  const res = await apiV2.GET('/api/agents/{slug}/projects/{ref}/', {
    params: { path: { slug, ref } },
  })
  return unwrap(res, 'getProject') as ProjectDetailOut
}

/** Workspace projects: owned by a workspace, kept current by a steward agent. */
export type AgentRosterRow = Schemas['AgentRosterRowOut']

// Every agent in the workspace with what it is doing (counts and recent turns).
export async function listAgentRoster(): Promise<AgentRosterRow[]> {
  const res = await apiV2.GET('/api/agent-roster/', {})
  return Array.from(unwrap(res, 'listAgentRoster')) as AgentRosterRow[]
}

// Every project across the agents the caller can see (one workspace on a tenant
// page). `status` defaults to active on the server; '' asks for all.
export async function listFleetProjects(status = ''): Promise<ProjectOut[]> {
  const res = await apiV2.GET('/api/projects/', { params: { query: { status } } })
  return Array.from(unwrap(res, 'listFleetProjects')) as ProjectOut[]
}

// `workspace` pins the read: the flat list 422s for someone whose workspaces
// span more than one organisation.
export async function listWorkspaceProjects(workspace?: string): Promise<ProjectOut[]> {
  const res = await apiV2.GET('/api/workspace-projects/', { params: { query: {} }, ...pin(workspace) })
  return Array.from(unwrap(res, 'listWorkspaceProjects')) as ProjectOut[]
}

export type McpServerOut = Schemas['McpServerOut']

/** The MCP servers a person could add: workspace projects serving their own tools, in
 *  `workspace` and the workspaces below it they belong to. Browsed, never auto-added. */
export async function listMcpServers(workspace: string): Promise<McpServerOut[]> {
  const res = await apiV2.GET('/api/mcp-servers/', pin(workspace))
  return Array.from(unwrap(res, 'listMcpServers')) as McpServerOut[]
}

/** One workspace project by its address, `<workspace>/<handle>`. */
export async function getWorkspaceProject(workspace: string, handle: string): Promise<ProjectDetailOut> {
  const res = await apiV2.GET('/api/workspace-projects/{workspace}/{handle}/', {
    params: { path: { workspace, handle } },
  })
  return unwrap(res, 'getWorkspaceProject') as ProjectDetailOut
}

/** A project's ideas (people's wants, not its tasks) and its contributors. */
export async function getProjectIdeas(slug: string, ref: string): Promise<ProjectIdeasOut> {
  const res = await apiV2.GET('/api/agents/{slug}/projects/{ref}/contributors/', {
    params: { path: { slug, ref } },
  })
  return unwrap(res, 'getProjectIdeas') as ProjectIdeasOut
}

/** A project as a teammate sees it: what it is, where it stands, its ideas and people. */
export async function getProjectTeam(slug: string, ref: string): Promise<ProjectTeamOut> {
  const res = await apiV2.GET('/api/agents/{slug}/projects/{ref}/team/', {
    params: { path: { slug, ref } },
  })
  return unwrap(res, 'getProjectTeam') as ProjectTeamOut
}

/** Add your own idea to a project, in your own words. */
export async function addIdea(slug: string, ref: string, body: { quote: string; title?: string }): Promise<IdeaCreatedOut> {
  const res = await apiV2.POST('/api/agents/{slug}/projects/{ref}/ideas/', {
    params: { path: { slug, ref } },
    body: { title: '', ...body },
  })
  return unwrap(res, 'addIdea') as IdeaCreatedOut
}

/** One contributor's page on a project. `person` is an id or an email address. */
export async function getContributorPage(
  slug: string,
  ref: string,
  person: string,
): Promise<ContributorPageOut> {
  const res = await apiV2.GET('/api/agents/{slug}/projects/{ref}/contributors/{person}/', {
    params: { path: { slug, ref, person } },
  })
  return unwrap(res, 'getContributorPage') as ContributorPageOut
}

/** Delete a contributor's page (the person, the agent's admins, or its writer). */
export async function deleteContributorPage(slug: string, ref: string, person: string): Promise<void> {
  const res = await apiV2.DELETE('/api/agents/{slug}/projects/{ref}/contributors/{person}/summary/', {
    params: { path: { slug, ref, person } },
  })
  if (res.error !== undefined) unwrap(res, 'deleteContributorPage')
}

export async function createProject(
  slug: string,
  body: { name: string; outcome: string },
): Promise<ProjectOut> {
  const res = await apiV2.POST('/api/agents/{slug}/projects/', {
    params: { path: { slug } },
    // The generated request type spells out every defaulted field as required
    // (Ninja emits required-with-default); fill them here, not at each caller.
    body: {
      ext_id: '',
      handle: '',
      priority: 0,
      status: 'active',
      owner_note: '',
      owner_email: '',
      drive_folder_id: '',
      drive_folder_url: '',
      repo_slug: '',
      notes: '',
      ...body,
    },
  })
  return unwrap(res, 'createProject') as ProjectOut
}

export async function patchProject(
  slug: string,
  ref: string,
  body: Schemas['AgentProjectPatch'],
): Promise<ProjectOut> {
  const res = await apiV2.PATCH('/api/agents/{slug}/projects/{ref}/', {
    params: { path: { slug, ref } },
    body,
  })
  return unwrap(res, 'patchProject') as ProjectOut
}

/** At most `limit` rows (server default 200). Unfiltered, the server puts the
 *  pending rows first, so a short page is "the whole queue + recent history". */
export async function listTaskActions(
  slug: string,
  opts: { status?: 'pending' | 'applied'; limit?: number } = {},
): Promise<TaskActionOut[]> {
  const query: { status?: string; limit?: number } = {}
  if (opts.status) query.status = opts.status
  if (opts.limit) query.limit = opts.limit
  const res = await apiV2.GET('/api/agents/{slug}/actions/', {
    params: { path: { slug }, query },
  })
  return Array.from(unwrap(res, 'listTaskActions')) as TaskActionOut[]
}

// The ordered runner-assignment API (the routing-matrix UI's read/write
// model) — supersedes the deprecated kind-based runner_preference above.
export async function getAgentRunners(slug: string, workspace?: string): Promise<AgentRunnerOut[]> {
  const res = await apiV2.GET('/api/agents/{slug}/runners', {
    params: { path: { slug } },
    ...(workspace ? { headers: { [WORKSPACE_HEADER]: workspace } } : {}),
  })
  return Array.from(unwrap(res, 'getAgentRunners'))
}

// Every runner the caller may start a chat with this agent on — its runner order
// first (by rank), then any other box they may choose (rank null). Choosing one
// pins the chat there past the order and any strict source rule.
export async function getAgentPinnableRunners(slug: string, workspace?: string): Promise<AgentPinnableRunnerOut[]> {
  const res = await apiV2.GET('/api/agents/{slug}/pinnable-runners', {
    params: { path: { slug } },
    ...(workspace ? { headers: { [WORKSPACE_HEADER]: workspace } } : {}),
  })
  return Array.from(unwrap(res, 'getAgentPinnableRunners'))
}

// Where a canopy-web chat from you with this agent would go right now with no
// runner chosen — what "Auto" means for it.
export async function getAgentRoutePreview(slug: string, workspace?: string): Promise<AgentRoutePreviewOut> {
  const res = await apiV2.GET('/api/agents/{slug}/route-preview', {
    params: { path: { slug } },
    ...(workspace ? { headers: { [WORKSPACE_HEADER]: workspace } } : {}),
  })
  return unwrap(res, 'getAgentRoutePreview') as unknown as AgentRoutePreviewOut
}

// Flip the agent's runtime autonomy posture (manual | auto) — the board-side
// switch the fleet turn procedure reads at preflight. A human decision; the
// agent's own repo publish can't touch it.
//
// Returns just the confirmed mode, not the whole AgentDetailOut the endpoint
// sends: openapi-fetch's Readable<T> degrades the response's nested
// `runner_preference` array into an ArrayLike-shaped object, so declaring
// AgentDetailOut here fails to compile (TS2719 — same root cause as toPage's
// note above). The mode is all any caller wants back from a toggle.
export async function setAgentTurnMode(slug: string, mode: TurnMode, workspace?: string): Promise<TurnMode> {
  const res = await apiV2.PATCH('/api/agents/{slug}/turn-mode', {
    params: { path: { slug } },
    ...(workspace ? { headers: { [WORKSPACE_HEADER]: workspace } } : {}),
    body: { turn_mode: mode },
  })
  return unwrap(res, 'setAgentTurnMode').turn_mode
}

// The agent hold: while held no runner starts the agent's turns (its schedules,
// dispatches, mail, chats with it); they wait queued for the release. Any editor
// may hold or release (agent.work); any member may read.
export type AgentHold = Schemas['AgentHoldOut']

export async function getAgentHold(slug: string): Promise<AgentHold> {
  const res = await apiV2.GET('/api/agents/{slug}/hold', { params: { path: { slug } } })
  return unwrap(res, 'getAgentHold')
}

export async function holdAgent(slug: string, note: string): Promise<AgentHold> {
  const res = await apiV2.POST('/api/agents/{slug}/hold', { params: { path: { slug } }, body: { note } })
  return unwrap(res, 'holdAgent')
}

export async function releaseAgentHold(slug: string): Promise<AgentHold> {
  const res = await apiV2.POST('/api/agents/{slug}/hold/release', { params: { path: { slug } } })
  return unwrap(res, 'releaseAgentHold')
}

// Turn Slack access to the agent on or off (owner only). Same return shape as
// setAgentTurnMode, for the same reason: the toggle only needs the flag back.
// The agent's owner or a workspace owner. `null` clears the owner (workspace
// owners only). Returns the refreshed detail.
export async function transferAgentOwner(slug: string, userId: number | null): Promise<AgentDetailOut> {
  const res = await apiV2.PUT('/api/agents/{slug}/owner', {
    params: { path: { slug } },
    body: { user_id: userId },
  })
  return unwrap(res, 'transferAgentOwner') as unknown as AgentDetailOut
}

/** Link this agent to its canopy login — always the account at its own email — or
 *  unlink it. The agent's owner or an admin. With no account at that email canopy
 *  creates one; an existing account there gets a request its person accepts
 *  (`canopy_user_request` on the answer). A refusal carries its reason. */
export async function linkAgentCanopyUser(slug: string, link: boolean): Promise<AgentDetailOut> {
  const res = await apiV2.PUT('/api/agents/{slug}/canopy-user', {
    params: { path: { slug } },
    body: { link },
  })
  return unwrap(res, 'Could not change the canopy user') as unknown as AgentDetailOut
}

/** A request, from an agent's admin, to make YOUR canopy account that agent's login. */
export type AgentLoginRequestOut = Schemas['AgentLoginRequestOut']

export async function listMyAgentLoginRequests(): Promise<AgentLoginRequestOut[]> {
  const res = await apiV2.GET('/api/me/agent-login-requests/')
  return unwrap(res, 'Could not load requests for your account') as unknown as AgentLoginRequestOut[]
}

/** Accept (browser + passkey step-up, answered by the API client) or decline. */
export async function decideMyAgentLoginRequest(id: number, accept: boolean): Promise<AgentLoginRequestOut> {
  const res = accept
    ? await apiV2.POST('/api/me/agent-login-requests/{request_id}/accept', { params: { path: { request_id: id } } })
    : await apiV2.POST('/api/me/agent-login-requests/{request_id}/decline', { params: { path: { request_id: id } } })
  return unwrap(res, accept ? 'Could not accept' : 'Could not decline') as unknown as AgentLoginRequestOut
}

export type SlackEnabledOut = Schemas['SlackEnabledOut']

// Also reports what happened to the agent's `/<slug>` command in Slack.
export async function setAgentSlackEnabled(slug: string, enabled: boolean): Promise<SlackEnabledOut> {
  const res = await apiV2.PATCH('/api/agents/{slug}/slack', {
    params: { path: { slug } },
    body: { slack_enabled: enabled },
  })
  return unwrap(res, 'setAgentSlackEnabled')
}


// Wholesale replace of an agent's ordered runner list — index = rank. Each
// row carries its own `enabled`: false keeps the row (rank preserved) but it
// never routes — the toggle that replaced the old remove-chip affordance.
export async function putAgentRunners(
  slug: string,
  rows: readonly { runnerId: string; enabled: boolean }[],
  workspace?: string,
): Promise<AgentRunnerOut[]> {
  const res = await apiV2.PUT('/api/agents/{slug}/runners', {
    params: { path: { slug } },
    ...(workspace ? { headers: { [WORKSPACE_HEADER]: workspace } } : {}),
    body: { runners: rows.map((r) => ({ runner_id: r.runnerId, enabled: r.enabled })) },
  })
  return Array.from(unwrap(res, 'putAgentRunners'))
}

// Per-source overrides on top of the default ordered list — one rule per source.
// A separate endpoint from putAgentRunners on purpose: both live in one table,
// and each write is scoped server-side so neither clobbers the other's rows.
export type AgentCredentialStatusOut = Schemas['AgentCredentialStatusOut']

/** Which declared refs are set — booleans and timestamps, never values. */
export async function getAgentCredentialStatus(slug: string): Promise<AgentCredentialStatusOut[]> {
  const res = await apiV2.GET('/api/agents/{slug}/credentials/status', {
    params: { path: { slug } },
  })
  return Array.from(unwrap(res, 'getAgentCredentialStatus'))
}

/** Upsert named secrets. NON-CLOBBERING — omitted refs are untouched, so a
 *  single-field edit cannot wipe the rest. Returns the masked status; there is
 *  deliberately no route that reads a value back into a browser. */
export async function setAgentCredentials(
  slug: string,
  values: Record<string, string>,
): Promise<AgentCredentialStatusOut[]> {
  const res = await apiV2.PUT('/api/agents/{slug}/credentials', {
    params: { path: { slug } },
    body: { values },
  })
  return Array.from(unwrap(res, 'setAgentCredentials'))
}

export async function deleteAgentCredential(
  slug: string,
  name: string,
): Promise<AgentCredentialStatusOut[]> {
  const res = await apiV2.DELETE('/api/agents/{slug}/credentials/{name}', {
    params: { path: { slug, name } },
  })
  return Array.from(unwrap(res, 'deleteAgentCredential'))
}

export type GoogleMintOut = Schemas['GoogleMintOut']

/** Start connecting this agent's Google account. The sign-in is finished on one of
 *  the agent's runners (canopy never holds the token), so this returns the sign-in,
 *  not a link: poll `getGoogleMint` until `authorize_url` is set, then navigate to it
 *  (a TOP-LEVEL navigation — a redirect followed inside fetch() shows nothing).
 *  A 409 `no_mint_runner` says no runner that may hold the agent is online. */
export async function startGoogleMint(slug: string, loginHint = ''): Promise<GoogleMintOut> {
  const res = await apiV2.POST('/api/agents/{slug}/google/mint', {
    params: { path: { slug } },
    body: { login_hint: loginHint },
  })
  return unwrap(res, 'startGoogleMint')
}

export async function getGoogleMint(slug: string, id: string): Promise<GoogleMintOut> {
  const res = await apiV2.GET('/api/agents/{slug}/google/mint/{mint_id}', {
    params: { path: { slug, mint_id: id } },
  })
  return unwrap(res, 'getGoogleMint')
}

/** Start connecting YOUR OWN Google account to an agent, as a credential you delegate
 *  to it (`google-delegated-<account>`). Finished on one of the agent's runners like
 *  `startGoogleMint`: poll `getGoogleDelegation` until `authorize_url` is set, then
 *  navigate to it. 403 `not_an_admin` unless you are one of the agent's admins; 409
 *  `no_mint_runner` when no runner that may hold a credential of yours is online. */
export async function startGoogleDelegation(
  slug: string,
  account: string,
  services: string[] = ['gmail'],
  sessionMinutes = 0,
): Promise<GoogleMintOut> {
  const res = await apiV2.POST('/api/agents/{slug}/google/delegate', {
    params: { path: { slug } },
    body: { account, services, session_minutes: sessionMinutes },
  })
  return unwrap(res, 'startGoogleDelegation')
}

export async function getGoogleDelegation(slug: string, id: string): Promise<GoogleMintOut> {
  const res = await apiV2.GET('/api/agents/{slug}/google/delegate/{mint_id}', {
    params: { path: { slug, mint_id: id } },
  })
  return unwrap(res, 'getGoogleDelegation')
}

// ---- GitHub: the owner's identity, lent to one agent ------------------------

export type AgentGitHubCheck = { repo: string; ok: boolean; detail: string }
export type AgentGitHub = Omit<components['schemas']['AgentGitHubOut'], 'checks'> & {
  checks: AgentGitHubCheck[]
}
// What openapi-fetch actually hands back: the array degraded to ArrayLike.
type AgentGitHubWire = Omit<AgentGitHub, 'checks'> & { checks?: ArrayLike<AgentGitHubCheck> | null }
type GitHubRes = { data?: AgentGitHubWire; error?: unknown }

// Copied across the boundary for the readonly-array reason above.
function toGitHub(w: AgentGitHubWire): AgentGitHub {
  return { ...w, checks: Array.from(w.checks ?? [], (c) => ({ ...c, detail: c.detail ?? '' })) }
}

/** A refusal's own words. The server says exactly what is wrong with a pasted
 *  token (wrong resource owner, repo not selected, no pull-request permission),
 *  and that sentence is the most useful thing the screen can show. */
function githubResult(res: GitHubRes, what: string): AgentGitHub {
  return toGitHub(unwrap(res, `${what} failed`))
}

export async function getAgentGitHub(slug: string): Promise<AgentGitHub> {
  const res = await apiV2.GET('/api/agents/{slug}/github', { params: { path: { slug } } })
  return githubResult(res as unknown as GitHubRes, 'Loading GitHub status')
}

export async function setAgentGitHub(slug: string, token: string): Promise<AgentGitHub> {
  const res = await apiV2.PUT('/api/agents/{slug}/github', {
    params: { path: { slug } },
    body: { token },
  })
  return githubResult(res as unknown as GitHubRes, 'Saving the token')
}

export async function checkAgentGitHub(slug: string): Promise<AgentGitHub> {
  const res = await apiV2.POST('/api/agents/{slug}/github/check', { params: { path: { slug } } })
  return githubResult(res as unknown as GitHubRes, 'The check')
}

export async function deleteAgentGitHub(slug: string): Promise<AgentGitHub> {
  const res = await apiV2.DELETE('/api/agents/{slug}/github', { params: { path: { slug } } })
  return githubResult(res as unknown as GitHubRes, 'Removing the token')
}

export type AgentDefaultOrderOut = components['schemas']['AgentDefaultOrderOut']

// What the agent's "everything else" runs on: its own list, or the workspace
// default order it follows (own=false), with the runners that order lists but
// this agent cannot use, named.
export async function getAgentDefaultOrder(slug: string, workspace?: string): Promise<AgentDefaultOrderOut> {
  const res = await apiV2.GET('/api/agents/{slug}/default-order', {
    params: { path: { slug } },
    ...(workspace ? { headers: { [WORKSPACE_HEADER]: workspace } } : {}),
  })
  return unwrap(res, 'getAgentDefaultOrder') as unknown as AgentDefaultOrderOut
}

export async function getAgentRunnerRules(slug: string, workspace?: string): Promise<AgentRunnerRuleOut[]> {
  const res = await apiV2.GET('/api/agents/{slug}/runner-rules', {
    params: { path: { slug } },
    ...(workspace ? { headers: { [WORKSPACE_HEADER]: workspace } } : {}),
  })
  return Array.from(unwrap(res, 'getAgentRunnerRules'))
}

export type RunnerRuleEdit = {
  source: string
  actor: string
  runnerIds: readonly string[]
  strict: boolean
  turnMode: RuleTurnMode
}

// Saves the rules ONE AT A TIME: only the rules this edit changed are sent, and a
// rule it dropped is deleted. Each rule belongs to one (source, person), and its
// boxes can be someone else's: saving them all at once made every save depend on
// rights over every box any rule names, which nobody holds once two people's
// boxes are involved (ACE, 2026-10-05, #1143). `prev` is the list as loaded.
export async function saveAgentRunnerRules(
  slug: string,
  prev: readonly AgentRunnerRuleOut[],
  next: readonly RunnerRuleEdit[],
  workspace?: string,
): Promise<AgentRunnerRuleOut[]> {
  const headers = workspace ? { headers: { [WORKSPACE_HEADER]: workspace } } : {}
  const key = (r: { source: string; actor: string }) => `${r.source}\u0000${r.actor}`
  const before = new Map<string, AgentRunnerRuleOut[]>()
  for (const row of [...prev].sort((a, b) => a.rank - b.rank)) {
    const k = key(row)
    before.set(k, [...(before.get(k) ?? []), row])
  }
  const wanted = new Set(next.map(key))

  for (const [k, rows] of before) {
    if (wanted.has(k)) continue
    const res = await apiV2.DELETE('/api/agents/{slug}/runner-rules/{source}', {
      params: { path: { slug, source: rows[0].source }, query: { actor: rows[0].actor } },
      ...headers,
    })
    if (res.error) unwrap(res, 'deleteAgentRunnerRule')
  }

  for (const r of next) {
    const old = before.get(key(r)) ?? []
    const unchanged =
      old.length === r.runnerIds.length &&
      old.every((o, i) => o.runner_id === r.runnerIds[i]) &&
      old[0]?.strict === r.strict &&
      (old[0]?.turn_mode ?? '') === r.turnMode
    if (unchanged) continue
    const res = await apiV2.PUT('/api/agents/{slug}/runner-rules/{source}', {
      params: {
        // Cast against the REQUEST type: AgentRunnerRuleOut.source is a plain
        // string (output schemas serialize what the DB holds).
        path: { slug, source: r.source as RoutableSource },
        // '' = anyone on this source. The server normalizes a pasted
        // "Name <addr>" and 422s anything that isn't address-shaped.
        query: { actor: r.actor },
      },
      ...headers,
      body: {
        // ORDER IS THE PREFERENCE: rank is the index.
        runners: r.runnerIds.map((id) => ({
          runner_id: id,
          // This editor has no per-runner disable; a runner it keeps keeps its
          // state, one it adds starts enabled.
          enabled: old.find((o) => o.runner_id === id)?.enabled ?? true,
        })),
        strict: r.strict,
        // '' = the rule says nothing about mode; the next row down decides.
        turn_mode: r.turnMode,
      },
    })
    unwrap(res, 'setAgentRunnerRule')
  }
  return getAgentRunnerRules(slug, workspace)
}

export type AgentAdminOut = Schemas['AgentAdminOut']

// Held to the same bar as ownership transfer (the agent's owner or a workspace
// owner): granting admin hands over the agent's credentials. Both return the refreshed admin list (the roster reloads
// getAgentAccess instead, since admin changes access as well as role).
// `workspace` pins the AGENT's tenant: a page showing agents from several
// workspaces (the agent topology) is under one /w/:ws/ URL, and the flat route
// would resolve the agent in that one and 404 on the rest.
export async function grantAgentAdmin(slug: string, userId: number, workspace?: string): Promise<AgentAdminOut[]> {
  const res = await apiV2.PUT('/api/agents/{slug}/admins/{user_id}', {
    params: { path: { slug, user_id: userId } },
    ...(workspace ? { headers: { [WORKSPACE_HEADER]: workspace } } : {}),
  })
  return unwrap(res, 'grantAgentAdmin') as unknown as AgentAdminOut[]
}

export async function revokeAgentAdmin(slug: string, userId: number, workspace?: string): Promise<AgentAdminOut[]> {
  const res = await apiV2.DELETE('/api/agents/{slug}/admins/{user_id}', {
    params: { path: { slug, user_id: userId } },
    ...(workspace ? { headers: { [WORKSPACE_HEADER]: workspace } } : {}),
  })
  return unwrap(res, 'revokeAgentAdmin') as unknown as AgentAdminOut[]
}

export type AgentAccessOut = Schemas['AgentAccessOut']
export type AgentAccessRowOut = Schemas['AgentAccessRowOut']

// Everyone's role on the agent, why, and what they reach — the owner, the
// admins (granted and implicit), and every other member, as the harness would
// decide it for them signed in.
export async function getAgentAccess(slug: string): Promise<AgentAccessOut> {
  const res = await apiV2.GET('/api/agents/{slug}/access', { params: { path: { slug } } })
  return unwrap(res, 'getAgentAccess') as unknown as AgentAccessOut
}

// THE ACCESS LIST: one list of (who, role) lines per agent, plus the workspace-wide
// default lines it inherits (docs/superpowers/specs/2026-10-30-agent-roles-and-access-list-design.md).
// A refused line answers 422 with a reason fit to show beside the form (the
// ApiError message is that `detail`); a duplicate is a 409 the same way.
export type AccessListOut = Schemas['AccessListOut']
export type AccessLineOut = Schemas['AccessLineOut']
export type AccessLineIn = Schemas['AccessLineIn']
export type AccessLinePatch = Schemas['AccessLinePatch']
export type AccessRoleOut = Schemas['AccessRoleOut']

export async function getAccessList(slug: string): Promise<AccessListOut> {
  const res = await apiV2.GET('/api/agents/{slug}/access-list', { params: { path: { slug } } })
  return unwrap(res, 'getAccessList') as unknown as AccessListOut
}

export async function addAccessLine(slug: string, line: AccessLineIn): Promise<AccessLineOut> {
  const res = await apiV2.POST('/api/agents/{slug}/access-list', { params: { path: { slug } }, body: line })
  return unwrap(res, 'addAccessLine') as unknown as AccessLineOut
}

export async function changeAccessLine(slug: string, id: number, patch: AccessLinePatch): Promise<AccessLineOut> {
  const res = await apiV2.PATCH('/api/agents/{slug}/access-list/{line_id}', {
    params: { path: { slug, line_id: id } },
    body: patch,
  })
  return unwrap(res, 'changeAccessLine') as unknown as AccessLineOut
}

export async function removeAccessLine(slug: string, id: number): Promise<void> {
  const res = await apiV2.DELETE('/api/agents/{slug}/access-list/{line_id}', {
    params: { path: { slug, line_id: id } },
  })
  ensureOk(res, 'removeAccessLine')
}

export async function setInheritWorkspaceDefaults(slug: string, inherit: boolean): Promise<AccessListOut> {
  const res = await apiV2.PUT('/api/agents/{slug}/inherit-workspace-defaults', {
    params: { path: { slug } },
    body: { inherit },
  })
  return unwrap(res, 'setInheritWorkspaceDefaults') as unknown as AccessListOut
}

// A workspace's default lines: every agent in it and below inherits them unless it
// opts out. Workspace admins change them; `editor` is the workspace owner's call.
export async function listAgentDefaults(workspace: string): Promise<AccessLineOut[]> {
  const res = await apiV2.GET('/api/workspaces/{slug}/agent-defaults', { params: { path: { slug: workspace } } })
  return unwrap(res, 'listAgentDefaults') as unknown as AccessLineOut[]
}

export async function addAgentDefault(workspace: string, line: AccessLineIn): Promise<AccessLineOut> {
  const res = await apiV2.POST('/api/workspaces/{slug}/agent-defaults', { params: { path: { slug: workspace } }, body: line })
  return unwrap(res, 'addAgentDefault') as unknown as AccessLineOut
}

export async function removeAgentDefault(workspace: string, id: number): Promise<void> {
  const res = await apiV2.DELETE('/api/workspaces/{slug}/agent-defaults/{line_id}', {
    params: { path: { slug: workspace, line_id: id } },
  })
  ensureOk(res, 'removeAgentDefault')
}

export type AgentInterfaceOut = Schemas['AgentInterfaceOut']

// What callers (anyone not the owner or an admin) may ask this agent for.
// Live state on canopy-web: owners/admins edit it here (saveAgentInterface).
export async function getAgentInterface(slug: string): Promise<AgentInterfaceOut> {
  const res = await apiV2.GET('/api/agents/{slug}/interface', { params: { path: { slug } } })
  return unwrap(res, 'getAgentInterface') as unknown as AgentInterfaceOut
}

// Save the interface as YAML (kept verbatim, comments and all). Owner/admin only;
// a 422 carries the parser's reason, which the editor shows as-is.
export async function saveAgentInterface(slug: string, source: string): Promise<AgentInterfaceOut> {
  const res = await apiV2.PUT('/api/agents/{slug}/interface', {
    params: { path: { slug } },
    body: { source },
  })
  return unwrap(res, 'saveAgentInterface') as unknown as AgentInterfaceOut
}

// Save the interface as the model the form edits (a parsed mapping, no YAML source kept).
// A 422 carries the parser's reason.
export async function saveAgentInterfaceModel(slug: string, model: object): Promise<AgentInterfaceOut> {
  const res = await apiV2.PUT('/api/agents/{slug}/interface', {
    params: { path: { slug } },
    body: { interface: model as { [key: string]: unknown } },
  })
  return unwrap(res, 'saveAgentInterfaceModel') as unknown as AgentInterfaceOut
}

export type AgentInterfaceValidateOut = Schemas['AgentInterfaceValidateOut']

// The server's parser, asked about a draft without saving it: every problem with the field it is
// about, and (when valid) the access summary, the parsed model and its YAML.
export async function validateAgentInterface(
  slug: string,
  draft: { source: string } | { interface: object },
): Promise<AgentInterfaceValidateOut> {
  const res = await apiV2.POST('/api/agents/{slug}/interface/validate', {
    params: { path: { slug } },
    body: ('source' in draft ? draft : { interface: draft.interface as { [key: string]: unknown } }),
  })
  return unwrap(res, 'validateAgentInterface') as unknown as AgentInterfaceValidateOut
}

// The sensitive-data switch for one capability: canopy writes (or removes) its
// `data: sensitive` in the saved YAML and saves it the way saveAgentInterface does, so
// the editor and the switch always agree.
export async function setCapabilitySensitive(slug: string, name: string, handlesSensitive: boolean): Promise<AgentInterfaceOut> {
  const res = await apiV2.PUT('/api/agents/{slug}/interface/capabilities/{name}/sensitive', {
    params: { path: { slug, name } },
    body: { handles_sensitive: handlesSensitive },
  })
  return unwrap(res, 'setCapabilitySensitive') as unknown as AgentInterfaceOut
}

export type SettingChangeOut = Schemas['SettingChangeOut']

// The audited changes to this agent's interface, newest first: every save and
// reset, from any client. Admins see everyone's; others see their own.
export async function getAgentInterfaceHistory(slug: string): Promise<SettingChangeOut[]> {
  const res = await apiV2.GET('/api/events/setting-changes', {
    params: { query: { target: slug, limit: 50 } },
  })
  const items = (unwrap(res, 'getAgentInterfaceHistory') as unknown as { items: SettingChangeOut[] }).items
  return items.filter(
    (c) =>
      ((c.operation === 'publish_interface' || c.operation === 'unpublish_interface' || c.operation === 'reset_interface') && c.target === slug) ||
      // The switch's row names the capability too: "<slug> / <capability>".
      (c.operation === 'set_capability_sensitive' && c.target.startsWith(`${slug} / `)),
  )
}

// Back to the default interface: the owner, the admins and the workspace's editors, and nobody else.
export async function resetAgentInterface(slug: string): Promise<AgentInterfaceOut> {
  const res = await apiV2.DELETE('/api/agents/{slug}/interface', { params: { path: { slug } } })
  return unwrap(res, 'resetAgentInterface') as unknown as AgentInterfaceOut
}

/** Remove an agent and everything hanging off it (editor and above). Its GitHub
 *  repository is not touched. */
export async function deleteAgent(slug: string): Promise<void> {
  const res = await apiV2.DELETE('/api/agents/{slug}/', { params: { path: { slug } } })
  ensureOk(res, 'Deleting the agent failed')
}

// ---- proposed projects: the decision card, backings, the view beacon ----------
// Shared by the project page and the huddle page (`components/projects`). Each
// takes the project's own `workspace`: a huddle page in one workspace lists
// projects whose agents may live in another one of the caller's.

export type ProposedProjectOut = Schemas['ProposedProjectOut']
/** A check-in's proposed task: Start / Decline are the task's own approve / decline. */
export type ProposedTaskOut = Schemas['ProposedTaskOut']
export type ProjectBackingOut = Schemas['ProjectBackingOut']
export type BackingStance = Schemas['ProjectBackingIn']['stance']

const pin = (workspace?: string) => (workspace ? { headers: { [WORKSPACE_HEADER]: workspace } } : {})

/** Approve / decline / reply on a proposed project's decision card (`actOnTask`, pinned). */
export async function actOnProposal(
  p: Pick<ProposedProjectOut, 'agent' | 'workspace' | 'decision_task'>,
  action: 'approve' | 'decline' | 'reply',
  comment = '',
): Promise<ActResult> {
  const res = await apiV2.POST('/api/agents/{slug}/tasks/{ref}/actions', {
    params: { path: { slug: p.agent, ref: p.decision_task ?? '' } },
    ...pin(p.workspace),
    body: { action, comment },
  })
  return unwrap(res, 'actOnProposal') as ActResult
}

/** Say where you stand on a project, as yourself (replaces your earlier stance). */
export async function backProject(
  p: Pick<ProposedProjectOut, 'agent' | 'workspace' | 'ref'>,
  stance: BackingStance,
  line = '',
): Promise<ProjectBackingOut> {
  const res = await apiV2.PUT('/api/agents/{slug}/projects/{ref}/backings/me/', {
    params: { path: { slug: p.agent, ref: p.ref } },
    ...pin(p.workspace),
    body: { stance, line },
  })
  return unwrap(res, 'backProject') as ProjectBackingOut
}

export async function unbackProject(p: Pick<ProposedProjectOut, 'agent' | 'workspace' | 'ref'>): Promise<void> {
  const res = await apiV2.DELETE('/api/agents/{slug}/projects/{ref}/backings/me/', {
    params: { path: { slug: p.agent, ref: p.ref } },
    ...pin(p.workspace),
  })
  if (res.error !== undefined) unwrap(res, 'unbackProject')
}

/** Start (approve) / decline / reply on a check-in's proposed task (`actOnTask`, pinned). */
export async function actOnTaskProposal(
  t: Pick<ProposedTaskOut, 'agent' | 'workspace' | 'ref'>,
  action: 'approve' | 'decline' | 'reply',
  comment = '',
): Promise<ActResult> {
  const res = await apiV2.POST('/api/agents/{slug}/tasks/{ref}/actions', {
    params: { path: { slug: t.agent, ref: t.ref } },
    ...pin(t.workspace),
    body: { action, comment },
  })
  return unwrap(res, 'actOnTaskProposal') as ActResult
}

type TaskKey = Pick<ProposedTaskOut, 'agent' | 'workspace' | 'ref' | 'project_ref'>

/** Say where you stand on a proposed task, as yourself (replaces your earlier stance). */
export async function backTask(t: TaskKey, stance: BackingStance, line = ''): Promise<ProjectBackingOut> {
  const res = await apiV2.PUT('/api/agents/{slug}/projects/{ref}/tasks/{task}/backings/me/', {
    params: { path: { slug: t.agent, ref: t.project_ref, task: t.ref } },
    ...pin(t.workspace),
    body: { stance, line },
  })
  return unwrap(res, 'backTask') as ProjectBackingOut
}

export async function unbackTask(t: TaskKey): Promise<void> {
  const res = await apiV2.DELETE('/api/agents/{slug}/projects/{ref}/tasks/{task}/backings/me/', {
    params: { path: { slug: t.agent, ref: t.project_ref, task: t.ref } },
    ...pin(t.workspace),
  })
  if (res.error !== undefined) unwrap(res, 'unbackTask')
}

/** The view beacon: once per person, per object, per UTC day (server-side). */
export async function recordEngagementView(
  target: { project: number; workspace?: string } | { huddle_id: string; workspace?: string },
): Promise<void> {
  const body = 'project' in target ? { project: target.project, huddle_id: '' } : { huddle_id: target.huddle_id }
  await apiV2.POST('/api/engagement/view', { ...pin(target.workspace), body })
}

// Point an agent at its GitHub repository (owner or admin) — the one field the
// setup checklist's first step asks for, without the create wizard.
export async function setAgentRepository(slug: string, repoUrl: string, repoRef = ''): Promise<AgentDetailOut> {
  const res = await apiV2.PATCH('/api/agents/{slug}/repository', {
    params: { path: { slug } },
    body: { repo_url: repoUrl, repo_ref: repoRef },
  })
  return normalizeAgentDetail(unwrap(res, 'setAgentRepository'))
}

/** Which agent CLIs an agent runs on, preferred first, and a model for each it names.
 *  The agent's owner or an admin. Refused (422, with why) for a CLI no runner serving
 *  the workspace has, or one a schedule still runs on. */
export async function setAgentClis(
  slug: string, clis: Cli[], cliModels: Record<string, string>,
): Promise<{ clis: string[]; cli_models: Record<string, string> }> {
  const res = await apiV2.PATCH('/api/agents/{slug}/clis', {
    params: { path: { slug } },
    body: { clis, cli_models: cliModels },
  })
  const a = unwrap(res, 'Could not save the agent CLIs')
  return { clis: Array.from(a.clis ?? []), cli_models: { ...(a.cli_models ?? {}) } }
}

export type AgentLicenseOut = Schemas['AgentLicenseOut']

// Who uses this agent DIRECTLY, in their own Claude Code (canopy-web#1704): every
// license — active, requested (the caller list does not allow it yet) or revoked —
// with the private_runners it ran on and when it was last used. Any member may read.
export async function listAgentLicenses(slug: string): Promise<AgentLicenseOut[]> {
  const res = await apiV2.GET('/api/agents/{slug}/licenses', { params: { path: { slug } } })
  return unwrap(res, 'listAgentLicenses') as unknown as AgentLicenseOut[]
}

// Agent admins only. Revoke is per person and never undone by the caller list;
// approve re-asks the caller list and is refused (409) unless it gives them the
// whole agent.
export async function revokeAgentLicense(slug: string, userId: number): Promise<AgentLicenseOut> {
  const res = await apiV2.POST('/api/agents/{slug}/licenses/{user_id}/revoke', {
    params: { path: { slug, user_id: userId } },
    body: { note: '' },
  })
  return unwrap(res, 'revokeAgentLicense') as unknown as AgentLicenseOut
}

export async function approveAgentLicense(slug: string, userId: number): Promise<AgentLicenseOut> {
  const res = await apiV2.POST('/api/agents/{slug}/licenses/{user_id}/approve', {
    params: { path: { slug, user_id: userId } },
  })
  return unwrap(res, 'approveAgentLicense') as unknown as AgentLicenseOut
}
