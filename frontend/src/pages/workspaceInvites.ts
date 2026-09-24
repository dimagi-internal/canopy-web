import type { InviteOut } from '@/api/workspaces'

// A dead invite (accepted/revoked/expired) still lives in the API's list —
// this page only surfaces the ones a human might still act on. Pure + exported
// so they're testable without a renderer (and out of the page module, which
// may export only components for fast refresh).
export function isInvitePending(inv: InviteOut, now: number = Date.now()): boolean {
  if (inv.accepted_at || inv.revoked_at) return false
  return new Date(inv.expires_at).getTime() > now
}

// Nobody has accepted it and nobody revoked it — pending OR expired. An
// expired invite stays on the page because it is exactly the one an owner
// wants to find: somebody never got round to it, and "New link" revives it.
export function isInviteOutstanding(inv: InviteOut): boolean {
  return !inv.accepted_at && !inv.revoked_at
}

const DAY_MS = 86_400_000

// "Expires in 3 days" / "Expired 2 days ago" — the one fact that decides
// whether the link you are about to copy still works.
export function inviteExpiryLabel(inv: InviteOut, now: number = Date.now()): string {
  const delta = new Date(inv.expires_at).getTime() - now
  const days = Math.round(Math.abs(delta) / DAY_MS)
  if (delta > 0) {
    if (days === 0) return 'Expires today'
    return `Expires in ${days} day${days === 1 ? '' : 's'}`
  }
  if (days === 0) return 'Expired today'
  return `Expired ${days} day${days === 1 ? '' : 's'} ago`
}

export type EmailStatus = NonNullable<InviteOut['email_status']>

// What the owner is told after a create/resend. Canopy emails the link, but a
// send can be off, throttled or fail — and then the owner must hear plainly
// that the person has NOT been told, with the link right there to send by hand.
export function emailOutcomeText(status: EmailStatus | null | undefined, email: string): string {
  switch (status) {
    case 'sent':
      return `Emailed the invite to ${email}. The link is below if you'd also like to send it another way.`
    case 'throttled':
      return `${email} was emailed this invite less than a minute ago, so it wasn't sent again.`
    case 'failed':
      return `Couldn't email ${email} — send them this link yourself (Slack, email, whatever you use).`
    default:
      return `Canopy didn't email this invite — send ${email} this link yourself (Slack, email, whatever you use).`
  }
}
