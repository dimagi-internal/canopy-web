// Agent Workspace API client — a thin, typed wrapper over the generated
// OpenAPI client. Response entity types alias the generated schemas, so this
// file cannot drift from the server. Workspace scoping is handled by apiV2's
// middleware (see WS_SCOPED_API_PREFIXES in ./client.v2), not here.
import { apiV2 } from './client.v2'
import type { components } from './generated'

type Schemas = components['schemas']

export type AgentOut = Schemas['AgentOut']
export type AgentDetailOut = Schemas['AgentDetailOut']
export type AgentTurnOut = Schemas['AgentTurnOut']
export type AgentSyncOut = Schemas['AgentSyncOut']
export type AgentWorkProductOut = Schemas['AgentWorkProductOut']
export type AgentSkillOut = Schemas['AgentSkillOut']
export type AgentTaskOut = Schemas['AgentTaskOut']
export type AgentProjectOut = Schemas['AgentProjectOut']
export type AgentTaskLink = Schemas['AgentTaskLink']
export type AgentCommandOut = Schemas['AgentTaskCommandOut']
export type PostCommandResult = Schemas['CommandResultOut']
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

export type AgentTaskStatus = AgentTaskOut['status']
export type AgentCommandKind = Schemas['AgentTaskCommandIn']['kind']

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
function unwrap<T>(res: { data?: T; error?: unknown }, what: string): T {
  if (res.error !== undefined || res.data === undefined) {
    throw new Error(`${what} failed: ${JSON.stringify(res.error ?? 'no data')}`)
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

export async function listAgentWorkProducts(
  slug: string,
  params: ListAgentsParams = {},
): Promise<Page<AgentWorkProductOut>> {
  const res = await apiV2.GET('/api/agents/{slug}/work-products/', {
    params: { path: { slug }, query: { limit: params.limit } },
  })
  const page = toPage(unwrap(res, 'listAgentWorkProducts'))
  // tags degrades the same way one level down (see toPage's comment).
  return {
    ...page,
    items: page.items.map((w) => ({
      ...w,
      tags: w.tags ? Array.from(w.tags) : undefined,
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

// Plain array, not paginated.
export async function listAgentTasks(slug: string): Promise<AgentTaskOut[]> {
  const res = await apiV2.GET('/api/agents/{slug}/tasks/', { params: { path: { slug } } })
  const items = Array.from(unwrap(res, 'listAgentTasks'))
  // links degrades the same way (see toPage's comment); rebuild it.
  return items.map((t) => ({ ...t, links: t.links ? Array.from(t.links) : undefined }))
}

export async function listWaitingTasks(slug: string): Promise<AgentTaskOut[]> {
  const res = await apiV2.GET('/api/agents/{slug}/tasks/waiting/', {
    params: { path: { slug } },
  })
  const items = Array.from(unwrap(res, 'listWaitingTasks'))
  return items.map((t) => ({ ...t, links: t.links ? Array.from(t.links) : [] }) as AgentTaskOut)
}

export async function listAgentProjects(
  slug: string,
  status = '',
): Promise<AgentProjectOut[]> {
  const res = await apiV2.GET('/api/agents/{slug}/projects/', {
    params: { path: { slug }, query: status ? { status } : {} },
  })
  const items = Array.from(unwrap(res, 'listAgentProjects'))
  // `links` degrades to a readonly tuple through the generated types, the same
  // way tasks do (see `toPage`); rebuild it so callers can treat it as an array.
  return items.map(
    (p) => ({ ...p, links: p.links ? Array.from(p.links) : [] }) as AgentProjectOut,
  )
}

export async function createAgentProject(
  slug: string,
  body: { name: string; outcome?: string; drive_folder_url?: string; repo_slug?: string },
): Promise<AgentProjectOut> {
  const res = await apiV2.POST('/api/agents/{slug}/projects/', {
    params: { path: { slug } },
    // The generated request type spells out every field; the server defaults
    // them all but the schema does not mark them optional, so they are filled
    // here rather than each caller repeating them.
    body: {
      ext_id: '',
      outcome: '',
      status: 'active',
      owner_note: '',
      drive_folder_id: '',
      drive_folder_url: '',
      repo_slug: '',
      notes: '',
      ...body,
    },
  })
  return unwrap(res, 'createAgentProject') as AgentProjectOut
}

export async function patchAgentProject(
  slug: string,
  ref: string,
  body: Partial<{ name: string; outcome: string; status: string; drive_folder_url: string }>,
): Promise<AgentProjectOut> {
  const res = await apiV2.PATCH('/api/agents/{slug}/projects/{ref}/', {
    params: { path: { slug, ref } },
    body,
  })
  return unwrap(res, 'patchAgentProject') as AgentProjectOut
}

export async function postTaskCommand(
  slug: string,
  taskId: number,
  kind: AgentCommandKind,
  payload?: Record<string, unknown>,
): Promise<PostCommandResult> {
  const res = await apiV2.POST('/api/agents/{slug}/tasks/{task_id}/commands', {
    params: { path: { slug, task_id: taskId } },
    // created_by is server-filled from request.user.email. The generated type
    // marks it required (Ninja emits required-with-default), so send "" and let
    // the server's `payload.created_by or request.user.email` take over — the
    // wart stops here rather than reaching every call site.
    body: { kind, payload: payload ?? {}, created_by: '' },
  })
  const data = unwrap(res, 'postTaskCommand')
  // task.links degrades the same way (see toPage's comment); rebuild it.
  return {
    ...data,
    task: data.task
      ? { ...data.task, links: data.task.links ? Array.from(data.task.links) : undefined }
      : data.task,
  }
}

export async function listAgentCommands(slug: string, status?: string): Promise<AgentCommandOut[]> {
  const res = await apiV2.GET('/api/agents/{slug}/commands', {
    params: { path: { slug }, query: { status } },
  })
  return Array.from(unwrap(res, 'listAgentCommands'))
}

export async function listPendingCommands(slug: string): Promise<AgentCommandOut[]> {
  return listAgentCommands(slug, 'pending')
}

// The ordered runner-assignment API (the routing-matrix UI's read/write
// model) — supersedes the deprecated kind-based runner_preference above.
export async function getAgentRunners(slug: string): Promise<AgentRunnerOut[]> {
  const res = await apiV2.GET('/api/agents/{slug}/runners', { params: { path: { slug } } })
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
export async function setAgentTurnMode(slug: string, mode: TurnMode): Promise<TurnMode> {
  const res = await apiV2.PATCH('/api/agents/{slug}/turn-mode', {
    params: { path: { slug } },
    body: { turn_mode: mode },
  })
  return unwrap(res, 'setAgentTurnMode').turn_mode
}

// Turn Slack access to the agent on or off (owner only). Same return shape as
// setAgentTurnMode, for the same reason: the toggle only needs the flag back.
// Browser-only: the server refuses this with any Authorization header, so no
// agent, plugin or assistant can move ownership. `null` clears the owner
// (workspace owners only). Returns the refreshed detail.
export async function transferAgentOwner(slug: string, userId: number | null): Promise<AgentDetailOut> {
  const res = await apiV2.PUT('/api/agents/{slug}/owner', {
    params: { path: { slug } },
    body: { user_id: userId },
  })
  return unwrap(res, 'transferAgentOwner') as unknown as AgentDetailOut
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
): Promise<AgentRunnerOut[]> {
  const res = await apiV2.PUT('/api/agents/{slug}/runners', {
    params: { path: { slug } },
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

export async function getAgentRunnerRules(slug: string): Promise<AgentRunnerRuleOut[]> {
  const res = await apiV2.GET('/api/agents/{slug}/runner-rules', { params: { path: { slug } } })
  return Array.from(unwrap(res, 'getAgentRunnerRules'))
}

export async function putAgentRunnerRules(
  slug: string,
  rules: readonly {
    source: string
    actor: string
    runnerIds: readonly string[]
    strict: boolean
    turnMode: RuleTurnMode
  }[],
): Promise<AgentRunnerRuleOut[]> {
  const res = await apiV2.PUT('/api/agents/{slug}/runner-rules', {
    params: { path: { slug } },
    body: {
      rules: rules.map((r) => ({
        // Cast against the REQUEST type, not the response's: AgentRunnerRuleOut
        // .source is a plain string (output schemas serialize what the DB holds),
        // so casting to it would type-check nothing.
        source: r.source as RoutableSource,
        // '' = any actor, which is what a rule meant before actors existed. The
        // server normalizes (a pasted "Name <addr>" header resolves to the bare
        // lowercase address) and 422s anything that isn't address-shaped.
        actor: r.actor,
        // ORDER IS THE PREFERENCE: rank is the index. A rule names several
        // runners because the operator's two macOS accounts alternate, so the
        // live one rotates (spec 2026-09-05).
        runners: r.runnerIds.map((id) => ({
          runner_id: id,
          // Always enabled: this editor has no per-runner disable affordance —
          // a runner you don't want in a rule is removed from it (cheap to
          // re-add). The server keeps `enabled` per row for the API's sake.
          enabled: true,
        })),
        strict: r.strict,
        // '' = the rule says nothing about mode; the next row down decides.
        turn_mode: r.turnMode,
      })),
    },
  })
  return Array.from(unwrap(res, 'putAgentRunnerRules'))
}

export type AgentAdminOut = Schemas['AgentAdminOut']

// Browser-only on the server, like ownership transfer: granting admin hands over
// the agent's credentials, so no token can do it. Both return the refreshed admin list (the roster reloads
// getAgentAccess instead, since admin changes access as well as role).
export async function grantAgentAdmin(slug: string, userId: number): Promise<AgentAdminOut[]> {
  const res = await apiV2.PUT('/api/agents/{slug}/admins/{user_id}', {
    params: { path: { slug, user_id: userId } },
  })
  return unwrap(res, 'grantAgentAdmin') as unknown as AgentAdminOut[]
}

export async function revokeAgentAdmin(slug: string, userId: number): Promise<AgentAdminOut[]> {
  const res = await apiV2.DELETE('/api/agents/{slug}/admins/{user_id}', {
    params: { path: { slug, user_id: userId } },
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
