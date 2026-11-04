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

/** An agent's own connection shared with that agent alone: no computers, and canopy-web only
 *  when the row has it on and this deployment accepts a share that names it. Null for a
 *  connection with no owner agent. */
export function agentOnlyChoice(state: ShareStateOut): ShareChoice | null {
  const agent = state.owner_agent?.id
  if (!agent) return null
  const server = state.server
  const web = !!server?.available && !!server.provider_calls && !!server.statements_enabled && !!server.on
  return { agents: [agent], myComputers: [], canopyWeb: web }
}

/** Whether `choice` can be stored without asking the owner anything: a changed key, a person
 *  vouching who is yet to be trusted, or no machine to seal to are for a person to decide. */
export function needsAPerson(state: ShareStateOut, choice: ShareChoice, trust: Trust): boolean {
  return !!changedEndorser(trust, choice)
    || vouchersToDecide(trust, choice).length > 0
    || leftOut(state, choice, trust).length > 0
    || chooseRecipients(state, choice, trust).length <= 1
}

/** A person closing the passkey prompt (or the browser refusing it without a click) is not a
 *  failure to report: the page asks again with a button. */
export function promptDismissed(e: unknown): boolean {
  return (e instanceof DOMException && (e.name === 'NotAllowedError' || e.name === 'AbortError'))
    || (e instanceof Error && /cancel/i.test(e.message))
}
