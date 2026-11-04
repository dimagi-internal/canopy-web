import { putShare, type ShareStateOut } from '@/api/connections'
import {
  buildShare, changedEndorser, chooseRecipients, leftOut, openForOwner, trustFor, vouchersToDecide,
  type OpenedAccount, type ShareChoice, type Trust,
} from './connectionShare'
import type { Unlocked } from './userKey'

// What the Share screen and the Connected screen both do with an owner's connection: open it
// with the unlocked vault key, then seal and PUT a share. The Share screen lets the owner pick
// the recipients; the Connected screen stores an agent's own connection for that agent alone,
// with no picker, through the same functions.

export interface OpenedOwned {
  account: OpenedAccount
  /** The statement the owner last signed, verified by `openForOwner`. */
  statement: Awaited<ReturnType<typeof openForOwner>>['statement']
  trust: Trust
}

/** Open `state` with the owner's key and read who this browser trusts for it. */
export async function openOwned(state: ShareStateOut, key: Unlocked, accept?: Set<string>): Promise<OpenedOwned> {
  const { account, statement } = await openForOwner(state, key)
  const trust = await trustFor(state, key, { accept })
  return { account, statement, trust }
}

/** Seal and send a share of `state` to `choice`; `trust` is the one the owner was shown. */
export async function putOwnerShare(state: ShareStateOut, key: Unlocked, choice: ShareChoice, trust: Trust): Promise<void> {
  const { share } = await buildShare(state, key, choice, trust)
  await putShare(state.id, share)
}

type Signed = { agents: string[]; my_computers: string[]; canopy_web?: string } | null | undefined

/** The share the Connected screen may store without asking: what the owner last signed (a
 *  reconnect must not narrow it), or on a first connect the owner agent alone. canopy-web is
 *  named when the row has it on or the earlier statement did, and only if it can be named now
 *  (never quietly dropped, which would turn project screens off). `ask` means a person has to
 *  look: something earlier is no longer choosable, or canopy-web can't be named. */
export type AutoChoice = { kind: 'ready'; choice: ShareChoice } | { kind: 'ask' }

export function autoChoice(state: ShareStateOut, before: Signed): AutoChoice {
  const owner = state.owner_agent?.id
  if (!owner) return { kind: 'ask' }
  const listedAgents = new Set(state.agents.map((a) => a.id))
  const listedMachines = new Set(state.candidates.filter((c) => c.kind === 'device' && !c.agent_id && c.mine).map((c) => c.target))
  const agents = [...new Set([owner, ...(before?.agents ?? [])])].sort()
  const computers = [...(before?.my_computers ?? [])].sort()
  if (!agents.every((a) => listedAgents.has(a) || a === owner) || !computers.every((c) => listedMachines.has(c))) {
    return { kind: 'ask' }
  }
  const server = state.server
  const want = !!server?.on || !!before?.canopy_web
  const canName = !!server?.available && !!server.provider_calls && !!server.statements_enabled
  if (want && !canName) return { kind: 'ask' }
  return { kind: 'ready', choice: { agents, myComputers: computers, canopyWeb: want } }
}

/** The earlier statement's key for canopy-web is not the one the row reports now. */
export function canopyWebKeyChanged(state: ShareStateOut, before: Signed, choice: ShareChoice): boolean {
  return !!before?.canopy_web && !!choice.canopyWeb && !!state.server?.thumbprint && state.server.thumbprint !== before.canopy_web
}

/** The row was reconnected since the owner last signed: a reconnect clears the writer's signature
 *  (canopy-web may then write its own while it is unshared). */
export function wasReconnected(state: ShareStateOut, before: Signed): boolean {
  const kind = (state.sealed_sig as { by?: { kind?: string } } | null | undefined)?.by?.kind
  return !!before && state.state === 'unshared' && (state.sealed_sig == null || kind === 'device')
}

function sameAsBefore(choice: ShareChoice, before: Signed): boolean {
  if (!before) return true
  const same = (a: string[], b: string[]) => a.length === b.length && a.every((x, i) => x === b[i])
  return same(choice.agents, [...before.agents].sort()) && same(choice.myComputers, [...before.my_computers].sort())
    && !!choice.canopyWeb === !!before.canopy_web
}

/** Why `choice` cannot be stored without a person: `ask` for a decision only they can make (a
 *  changed key, a voucher to decide, a reconnect that would sign something different), `none`
 *  when there is no machine to seal to yet. `ready` otherwise. */
export type Review = 'ready' | 'ask' | 'none'

export function reviewShare(state: ShareStateOut, choice: ShareChoice, trust: Trust, before: Signed): Review {
  if (changedEndorser(trust, choice) || vouchersToDecide(trust, choice).length > 0 || leftOut(state, choice, trust).length > 0) return 'ask'
  if (canopyWebKeyChanged(state, before, choice)) return 'ask'
  if (wasReconnected(state, before) && !sameAsBefore(choice, before)) return 'ask'
  if (chooseRecipients(state, choice, trust).length <= 1) return 'none'
  return 'ready'
}

/** A person closing the passkey prompt (or the browser refusing it without a click) is not a
 *  failure to report: the page asks again with a button. */
export function promptDismissed(e: unknown): boolean {
  return (e instanceof DOMException && (e.name === 'NotAllowedError' || e.name === 'AbortError'))
    || (e instanceof Error && /cancel/i.test(e.message))
}
