/**
 * A dynamic view asking for the viewer's own account to be lent to an agent, and ended
 * (`canopy.connections` in a view). The view only asks: it never sees a token, and the
 * connect is a full-page trip through the provider's sign-in and canopy's share screen,
 * where the viewer sees the limits and touches their passkey.
 */
import { connectionStartPath, disconnectMine, listMyConnections, type DisconnectOut } from '@/api/connections'

export interface LendRequest {
  agent?: string
  provider?: string
  minutes?: number
  ownerTurnsOnly?: boolean
}

export interface LendAsk {
  agent: string
  provider: string
  minutes?: number
  ownerTurnsOnly: boolean
}

const MIN_MINUTES = 5
const MAX_MINUTES = 7 * 24 * 60

/** The ask, or why it is refused: only for an agent the view declares tasks for. */
export function checkConnect(req: LendRequest, declaredAgents: readonly string[]): LendAsk | string {
  const agent = String(req.agent || '')
  if (!declaredAgents.includes(agent)) return `this view declares no task for ${agent || 'that agent'}`
  const provider = String(req.provider || '')
  if (!/^[a-z][a-z0-9_-]{0,39}$/.test(provider)) return 'which provider?'
  if (req.minutes === undefined) return { agent, provider, ownerTurnsOnly: !!req.ownerTurnsOnly }
  const minutes = Math.round(Number(req.minutes))
  if (!(minutes >= MIN_MINUTES && minutes <= MAX_MINUTES)) return `a lend lasts ${MIN_MINUTES} to ${MAX_MINUTES} minutes`
  return { agent, provider, minutes, ownerTurnsOnly: !!req.ownerTurnsOnly }
}

/** Where the connect starts: comes back to `returnTo`, this view's own page. */
export function lendStartPath(ask: LendAsk, workspace: string, returnTo: string): string {
  const q = new URLSearchParams({ for_agent: ask.agent })
  if (ask.minutes !== undefined) q.set('lend_minutes', String(ask.minutes))
  if (ask.ownerTurnsOnly) q.set('owner_turns_only', '1')
  return `${connectionStartPath(ask.provider, workspace, returnTo)}&${q.toString()}`
}

/** How a connect that returned to this page went, from the query the server added. */
export function connectOutcome(search: string): { provider: string; status: string; reason: string } | null {
  const q = new URLSearchParams(search)
  const provider = q.get('connection')
  return provider ? { provider, status: q.get('status') ?? '', reason: q.get('reason') ?? '' } : null
}

/** End one of the viewer's connections, if it is lent to `agent` and the view declares that
 *  agent, or has not been shared with anyone yet. The server's own check is that it is theirs. */
export async function disconnectLent(id: string, agent: string, declaredAgents: readonly string[]): Promise<DisconnectOut['outcome']> {
  if (!declaredAgents.includes(agent)) throw new Error(`this view declares no task for ${agent || 'that agent'}`)
  const row = (await listMyConnections()).connections.find((c) => c.id === id)
  if (!row) throw new Error('no such connection of yours')
  if (row.state !== 'unshared' && !row.statement.agents?.includes(agent)) throw new Error(`that connection is not lent to ${agent}`)
  return (await disconnectMine(id)).outcome
}
