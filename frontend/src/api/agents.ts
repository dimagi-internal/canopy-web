// Agent Workspace API client — a thin, typed wrapper over the generated
// OpenAPI client. Response entity types alias the generated schemas, so this
// file cannot drift from the server. Workspace scoping is handled by apiV2's
// middleware (see WS_SCOPED_API_PREFIXES in ./client.v2), not here.
import { apiV2, WORKSPACE_HEADER } from './client.v2'
import type { components } from './generated'

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
export type AgentRunnerOut = Schemas['AgentRunnerOut']
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

// openapi-fetch returns { data, error }. Every call here is a read or a command
// post whose failure is a bug, not a user-facing state — so unwrap and throw. A
// 401 never reaches here: apiV2's middleware redirects to login first.
//
// The thrown error carries the HTTP status and the server's `detail`, because a
// few failures ARE user-facing on a task card: acting on an already-closed ask
// (409 — the card refetches) and an empty reply (422).
export class AgentApiError extends Error {
  status: number
  detail: string
  constructor(message: string, status: number, detail: string) {
    super(message)
    this.status = status
    this.detail = detail
  }
}

function unwrap<T>(
  res: { data?: T; error?: unknown; response?: { status: number } },
  what: string,
): T {
  if (res.error !== undefined || res.data === undefined) {
    const err = res.error as { detail?: unknown } | undefined
    // Ninja sends a string detail for HttpError, and a list of
    // `{loc, msg, type}` for a schema validation 422 — show the first `msg`.
    const raw = err?.detail
    const first = Array.isArray(raw) ? (raw[0] as { msg?: unknown } | undefined)?.msg : raw
    const detail = typeof first === 'string' ? first : ''
    throw new AgentApiError(
      `${what} failed: ${JSON.stringify(res.error ?? 'no data')}`,
      res.response?.status ?? 0,
      detail,
    )
  }
  return res.data
}

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
  const page = toPage(unwrap(res, 'listAgents'))
  // runner_preference degrades the same way one level down (see toPage's comment);
  // rebuild each item.
  return {
    ...page,
    items: page.items.map((a) => ({
      ...a,
      runner_preference: a.runner_preference ? Array.from(a.runner_preference) : undefined,
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
): Promise<ActResult> {
  const res = await apiV2.POST('/api/agents/{slug}/tasks/{ref}/actions', {
    params: { path: { slug, ref } },
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
      status: 'active',
      owner_note: '',
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

/** Link this agent to the canopy user it IS (null unlinks). The agent's owner or
 *  an admin. A refusal carries its reason (already another instance's user, not a
 *  workspace member), which is what the control shows. */
export async function linkAgentCanopyUser(slug: string, userId: number | null): Promise<AgentDetailOut> {
  const res = await apiV2.PUT('/api/agents/{slug}/canopy-user', {
    params: { path: { slug } },
    body: { user_id: userId },
  })
  const out = res as unknown as { data?: AgentDetailOut; error?: { detail?: unknown } }
  if (out.error !== undefined || out.data === undefined) {
    const detail = out.error?.detail
    throw new Error(typeof detail === 'string' && detail ? detail : 'Could not change the canopy user')
  }
  return out.data
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

/** Start the browser mint for this agent's Google mailbox.
 *
 *  Returns the URL instead of navigating, because the caller must do a TOP-LEVEL
 *  navigation: a 302 followed inside fetch() resolves on Google's HTML and shows
 *  the user nothing at all. */
export async function startGoogleMint(slug: string): Promise<string> {
  const res = await apiV2.GET('/api/agents/{slug}/google/authorize', {
    params: { path: { slug } },
  })
  return unwrap(res, 'startGoogleMint').url
}

export async function getAgentVault(slug: string) {
  const res = await apiV2.GET('/api/agents/{slug}/vault', { params: { path: { slug } } })
  return unwrap(res, 'getAgentVault')
}

/** Set the vault name and/or its service key. NON-CLOBBERING on the key:
 *  omitting it leaves the stored one alone, so renaming a vault cannot silently
 *  de-provision the agent. */
export async function setAgentVault(slug: string, body: { vault?: string; service_key?: string }) {
  const res = await apiV2.PUT('/api/agents/{slug}/vault', {
    params: { path: { slug } },
    body,
  })
  return unwrap(res, 'setAgentVault')
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
  if (res.error !== undefined || res.data === undefined) {
    const detail = (res.error as { detail?: unknown } | undefined)?.detail
    throw new Error(typeof detail === 'string' && detail ? detail : `${what} failed`)
  }
  return toGitHub(res.data)
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

export async function unpublishAgentInterface(slug: string): Promise<AgentInterfaceOut> {
  const res = await apiV2.DELETE('/api/agents/{slug}/interface', { params: { path: { slug } } })
  return unwrap(res, 'unpublishAgentInterface') as unknown as AgentInterfaceOut
}
