// Pure helpers behind the "Connect Google mailbox" button.
//
// The mint exists because provisioning a mailbox was the last thing that could
// not be done from a browser: it meant a terminal, `gog auth login`, a loopback
// listener, and a hand-written secret. Jonathan's goal on 2026-09-05 was
// that "someone could plausibly create a completely new agent without direct
// access to the cloud box" — this is the piece that was missing.

/** The credential slot a Google mailbox lands in — the same one a hand-minted
 *  token uses, so the box never has two sources of truth for one mailbox. */
export const GOG_TOKEN_REF = 'gog-token'

/** Does this agent have a Google mailbox to connect? Either it declares the
 *  `gog-token` slot, or the agent simply HAS a mailbox address.
 *
 *  It used to be the declaration alone, so the button rendered only for an agent
 *  that declared secrets — ace — and never for ada/echo/eva/hal (declared: 0), which
 *  all have mailboxes and all run on the same `gog-token` slot. For those four the
 *  only way to re-mint was a terminal `gog login` plus a hand-written secret, which is
 *  the exact trip this button exists to remove (2026-10-04). */
export function declaresMailbox(rows: readonly { name: string }[], email?: string | null): boolean {
  return Boolean(email?.trim()) || rows.some((r) => r.name === GOG_TOKEN_REF)
}

/** Poll a sign-in until `done(it)` or `timeoutMs` passes; the last read, or null when
 *  it never got there. The runner reads its work on a 15-second tick, so each step of a
 *  sign-in takes up to that long. */
export async function followMint<T>(
  read: () => Promise<T>,
  done: (m: T) => boolean,
  { intervalMs = 2000, timeoutMs = 90_000 }: { intervalMs?: number; timeoutMs?: number } = {},
): Promise<T | null> {
  const until = Date.now() + timeoutMs
  for (;;) {
    const m = await read()
    if (done(m)) return m
    if (Date.now() + intervalMs > until) return null
    await new Promise((r) => setTimeout(r, intervalMs))
  }
}

/** What the return trip carried besides its status, for the outcomes that name names. */
export interface MintContext {
  /** `?account=`: the Google account that consented (sent with `wrong-account`). */
  account?: string | null
  /** The account the agent must be signed in as (`?expected=`, else its address). */
  agentEmail?: string | null
}

export interface MintOutcome {
  tone: 'ok' | 'error'
  text: string
  /** Where the fix is, when it is somewhere else. */
  link?: { href: string; label: string }
}

/** What `?google=<status>` on the return trip means, in the operator's terms.
 *  `null` = nothing to say (no param, or one we do not recognise). */
export function mintOutcome(status: string | null, ctx: MintContext = {}): MintOutcome | null {
  switch (status) {
    case 'ok':
      return { tone: 'ok', text: 'Mailbox connected. The token was encrypted on the agent’s runner (canopy never saw it); its runners pick it up on their next bootstrap.' }
    case 'pending':
      return { tone: 'ok', text: 'Google answered — the agent’s runner is finishing the sign-in…' }
    case 'expired':
      return { tone: 'error', text: 'This sign-in expired before it finished. Nothing was stored — start again.' }
    case 'superseded':
      return { tone: 'error', text: 'A newer sign-in replaced this one.' }
    case 'interrupted':
      return { tone: 'error', text: 'The runner restarted during the sign-in, so it could not finish. Nothing was stored — start again.' }
    case 'no-runner':
      return { tone: 'error', text: 'The runner finishing the sign-in may no longer hold this agent. Nothing was stored — start again.' }
    case 'no-client':
      return { tone: 'error', text: 'The runner could not read the Google OAuth client from the agent’s vault (the workspace’s shared gog-oauth-client-web), so nothing was stored. Give the runner a key to that vault, then try again.' }
    case 'store-failed':
    case 'bad-request':
      return { tone: 'error', text: 'The runner finished the sign-in but the token could not be stored. Nothing changed — try again.' }
    case 'denied':
      return { tone: 'error', text: 'Sign-in was cancelled, so nothing changed.' }
    case 'no-refresh-token':
      // Worth its own message rather than a generic failure: it is the one
      // outcome that would otherwise LOOK like success. Google withholds the
      // refresh token for an already-consented account, and a token without one
      // imports cleanly and can never refresh.
      return { tone: 'error', text: 'Google returned no refresh token — the account had already granted access. Remove canopy-web under the account’s third-party access and try again.' }
    case 'no-email':
      return { tone: 'error', text: 'Google did not return which mailbox consented, so nothing was stored.' }
    case 'exchange-failed':
      return { tone: 'error', text: 'The runner’s token exchange with Google failed. Nothing was stored — try again.' }
    case 'wrong-account': {
      // The runner reads a Gmail agent's mail as agent.email, so a token for any other
      // account would never serve it; the server stored nothing.
      const who = ctx.account?.trim() || 'a different account'
      const agent = ctx.agentEmail?.trim() || 'its own mailbox'
      return {
        tone: 'error',
        text: `Google signed in as ${who}, but this agent is ${agent}. Sign in to Google as the agent and try again.`,
      }
    }
    case 'zk-no-recipients':
      // A zero-knowledge agent: the token is sealed to its devices and members,
      // and it has neither yet (canopy-web#1550).
      // The setup checklist puts the vault step first for exactly this reason, and
      // the link is that step's own.
      return {
        tone: 'error',
        text: 'This agent’s vault has no keys yet, so nothing was stored — set up its vault (passkey) first, then connect Google.',
        link: { href: '/settings#vault-key', label: 'Set up your vault key' },
      }
    default:
      return null
  }
}
