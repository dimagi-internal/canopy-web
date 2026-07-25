// Harness API client — the runner registry and turn lifecycle. Separate from
// ./agents because a Runner is not an agent: the harness is framework tier and
// the agent surface is product tier (see ARCHITECTURE.md).
import { apiV2, WORKSPACE_HEADER } from './client.v2'
import type { components } from './generated'

export type RunnerOut = components['schemas']['RunnerOut']
export type TurnOut = components['schemas']['TurnOut']

function unwrap<T>(res: { data?: T; error?: unknown }, what: string): T {
  if (res.error !== undefined || res.data === undefined) {
    throw new Error(`${what} failed: ${JSON.stringify(res.error ?? 'no data')}`)
  }
  return res.data
}

export async function listRunners(): Promise<RunnerOut[]> {
  const res = await apiV2.GET('/api/harness/runners/')
  // openapi-fetch's Readable<T> helper degrades `readonly Foo[]` into an
  // ArrayLike-shaped object (numeric index + length, no Symbol.iterator) — see
  // ./agents.ts's toPage comment for the full explanation. Array.from rebuilds
  // a real array rather than casting.
  return Array.from(unwrap(res, 'listRunners'))
}

// Dispatch a turn from the phone composer — to an agent OR a repo.
//
// An AGENT turn routes through the flat mount: the server derives the agent's
// single workspace, so there is nothing to disambiguate.
//
// A REPO turn's tenant is FIRST-CLASS — the turn is owned by a workspace, and the
// caller says which. `/supervisor` is not a tenant surface, so we pin the
// workspace explicitly via WORKSPACE_HEADER, which the v2 client rewrites onto
// /api/w/{ws}/harness/turns/ (the server gates membership there). No hidden
// default: the composer chooses the workspace (defaulting its VALUE to dimagi),
// so repo→workspace ownership stays explicit and survives into cloud/multiplayer.
export type UnclaimableTurn = components['schemas']['UnclaimableTurnOut']

/** Queued turns no online runner can claim — a silent stall unless surfaced. */
export async function listUnclaimableTurns(): Promise<UnclaimableTurn[]> {
  const res = await apiV2.GET('/api/harness/turns/unclaimable')
  // Array.from for the same Readable<T> reason as listRunners above.
  return Array.from(unwrap(res, 'listUnclaimableTurns'))
}

export async function enqueueTurn(input: {
  agentSlug?: string
  project?: string
  workspace?: string
  prompt: string
  threadKey?: string
}): Promise<TurnOut> {
  const { agentSlug = '', project = '', workspace = '', prompt, threadKey = '' } = input
  const target = agentSlug || project
  // A stable-enough idempotency key: a double-tap of Send within the same second
  // collapses server-side rather than firing twice.
  const key = `cmd-${target}-${Date.now()}`
  const body = {
    agent_slug: agentSlug,
    project,
    origin: 'manual',
    idempotency_key: key,
    prompt,
    // A thread_key makes the runner REUSE one session across dispatches (the
    // phone owns a persistent thread per target) rather than forking a fresh
    // emdash task each time. The runner reads origin_ref.thread_key.
    origin_ref: threadKey ? { thread_key: threadKey } : {},
    routing: 'prefer_local',
    // `satisfies` keeps the origin/routing string literals narrow so they match the
    // generated enum unions (origin: board|api|…; routing: prefer_local|…) instead
    // of widening to `string`, and flags any future TurnIn schema drift here.
  } satisfies components['schemas']['TurnIn']
  // A repo turn must land in its owning workspace; pin it explicitly.
  const init = project
    ? { body, headers: { [WORKSPACE_HEADER]: workspace } }
    : { body }
  const res = await apiV2.POST('/api/harness/turns/', init)
  return unwrap(res, 'enqueueTurn')
}

// Poll a turn's lifecycle — the Continue flow watches queued -> running (i.e. the
// runner has claimed it and is delivering the prompt into the live session).
export async function getTurn(turnId: string): Promise<TurnOut> {
  const res = await apiV2.GET('/api/harness/turns/{turn_id}', {
    params: { path: { turn_id: turnId } },
  })
  return unwrap(res, 'getTurn')
}

// Un-queue a misfired turn (queued only; the server 409s a running turn).
export async function cancelTurn(turnId: string): Promise<TurnOut> {
  const res = await apiV2.POST('/api/harness/turns/{turn_id}/cancel', {
    params: { path: { turn_id: turnId } },
  })
  return unwrap(res, 'cancelTurn')
}
