// Pure helpers behind the "Connect Google mailbox" button.
//
// The mint exists because provisioning a mailbox was the last thing that could
// not be done from a browser: it meant a terminal, `gog auth login`, a loopback
// listener, and a hand-written 1Password item. Jonathan's goal on 2026-09-05 was
// that "someone could plausibly create a completely new agent without direct
// access to the cloud box or 1pass" — this is the piece that was missing.

/** The credential slot a Google mailbox lands in — the same one a hand-minted
 *  token uses, so the box never has two sources of truth for one mailbox. */
export const GOG_TOKEN_REF = 'gog-token'

/** Does this agent have a Google mailbox to connect? Either its runtime.yaml
 *  declares the `gog-token` slot, or the agent simply HAS a mailbox address.
 *
 *  It used to be the declaration alone, so the button rendered only for an agent
 *  with a runtime.yaml — ace — and never for ada/echo/eva/hal (declared: 0), which
 *  all have mailboxes and all run on the same `gog-token` slot. For those four the
 *  only way to re-mint was a terminal `gog login` plus a 1Password write, which is
 *  the exact trip this button exists to remove (2026-10-04). */
export function declaresMailbox(rows: readonly { name: string }[], email?: string | null): boolean {
  return Boolean(email?.trim()) || rows.some((r) => r.name === GOG_TOKEN_REF)
}

/** What the return trip carried besides its status, for the outcomes that name names. */
export interface MintContext {
  /** `?account=`: the Google account that consented (sent with `wrong-account`). */
  account?: string | null
  /** The agent's own address — the account a Gmail agent must be signed in as. */
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
      return { tone: 'ok', text: 'Mailbox connected. The token is stored and a runner will pick it up on its next bootstrap.' }
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
      return { tone: 'error', text: 'The token exchange with Google failed. Nothing was stored — try again.' }
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
