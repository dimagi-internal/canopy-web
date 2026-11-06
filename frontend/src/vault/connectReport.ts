/** How an agent's connect ended, said on screen and reported where someone can read it later.
 *
 * Opening the vault key happens only in the browser, so when a connect fails there the server
 * otherwise sees nothing at all. Every connect attempt reports how it ended, twice, both
 * fire-and-forget and secret-free (an error's name and message, the step, never a key or token):
 *  - the server log (`vault.client_report`, beside the browser, platform and keyset facts);
 *  - the workspace event log (`source: connections.connect`, keyed by connection), which the
 *    workspace's admins and their agents can read back with `list_events`.
 */
import { reportVaultClient } from '@/api/vault'
import { recordEvents } from '@/api/logs'
import { PrfUnsupportedError } from './prf'

/** Where the attempt was: `preflight` is the Connect tap before the provider's sign-in; the rest
 *  are the Connected page finishing it (open the vault key, open the sign-in, check who gets it,
 *  store it). */
export type ConnectStep = 'preflight' | 'unlock' | 'open' | 'review' | 'store'
/** `arrival`: the Connected page's own try, with no tap. `tap`: a button the person pressed. */
export type ConnectTrigger = 'arrival' | 'tap'
export type ConnectOutcome = 'ok' | 'failed' | 'cancelled' | 'unsupported' | 'needs_decision' | 'no_machines'

export interface ConnectReport {
  /** The connection's id; on the Connect tap there is none yet, so the provider stands in. */
  key: string
  agent: string
  step: ConnectStep
  trigger: ConnectTrigger
  outcome: ConnectOutcome
  error?: unknown
  /** On success: the machines it was stored for. */
  machines?: string[]
}

export function errorText(e: unknown): string {
  if (e === undefined || e === null) return ''
  if (e instanceof Error) return `${e.name}: ${e.message}`.slice(0, 300)
  return String(e).slice(0, 300)
}

export function outcomeOf(e: unknown): ConnectOutcome {
  if (e instanceof PrfUnsupportedError) return 'unsupported'
  if (e instanceof DOMException && (e.name === 'NotAllowedError' || e.name === 'AbortError')) return 'cancelled'
  return 'failed'
}

/** The sentence a person reads for a failed step: what the browser actually said, in plain words. */
export function failureMessage(e: unknown, trigger: ConnectTrigger): string {
  if (e instanceof DOMException && e.name === 'NotAllowedError') {
    return trigger === 'arrival'
      ? "Your browser didn't open the passkey prompt by itself (some phones and password managers only do it after a tap). Tap Try again."
      : 'The passkey prompt closed before it finished: it was cancelled, timed out, or the browser blocked it. Tap Try again.'
  }
  if (e instanceof DOMException && e.name === 'AbortError') return 'The passkey prompt was cancelled. Tap Try again.'
  if (e instanceof DOMException && e.name === 'InvalidStateError') {
    return 'This passkey cannot be used here. Use the passkey marked ✓ under Settings → Security.'
  }
  if (e instanceof DOMException && e.name === 'SecurityError') {
    return "The browser refused the passkey for this site's address. Open canopy-web at its usual address and try again."
  }
  if (e instanceof Error && e.message) return e.message
  return 'Could not store it.'
}

export function reportConnect(r: ConnectReport): void {
  const outcome = r.outcome
  const error = errorText(r.error)
  const vaultOutcome = outcome === 'ok' || outcome === 'cancelled' || outcome === 'unsupported' ? outcome : 'failed'
  reportVaultClient({
    stage: `connect:${r.step}:${r.trigger}`.slice(0, 40),
    method: 'passkey',
    outcome: vaultOutcome,
    error: [outcome !== vaultOutcome ? outcome : '', error, r.machines?.length ? `machines=${r.machines.join('/')}` : '']
      .filter(Boolean).join(' '),
  })
  const level = outcome === 'ok' ? 'info' : outcome === 'cancelled' ? 'warn' : 'error'
  const summary = outcome === 'ok'
    ? `Connected ${r.agent}${r.machines?.length ? ` on ${r.machines.join(', ')}` : ''}`
    : `Connecting ${r.agent} stopped at ${r.step} (${r.trigger}): ${outcome}${error ? ` — ${error}` : ''}`
  void recordEvents([{
    source: 'connections.connect',
    kind: outcome,
    level,
    // A blank key never coalesces; a key per attempt keeps every attempt as its own row.
    key: `${r.key}:${Date.now()}`,
    summary: summary.slice(0, 500),
    payload: {
      connection: r.key, agent: r.agent, step: r.step, trigger: r.trigger, outcome, error,
      machines: r.machines ?? [], page: window.location.pathname.slice(0, 100),
      user_agent: navigator.userAgent.slice(0, 200),
    },
  }]).catch(() => undefined)
}
