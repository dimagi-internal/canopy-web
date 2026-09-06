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

export function declaresMailbox(rows: readonly { name: string }[]): boolean {
  return rows.some((r) => r.name === GOG_TOKEN_REF)
}

/** What `?google=<status>` on the return trip means, in the operator's terms.
 *  `null` = nothing to say (no param, or one we do not recognise). */
export function mintOutcome(status: string | null): { tone: 'ok' | 'error'; text: string } | null {
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
    default:
      return null
  }
}
