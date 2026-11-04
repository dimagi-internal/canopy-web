import type { HostLink } from './hostLink'

/** Tell canopy what this page can do and what it is showing.
 *
 *  Module-level, not a hook, because BOTH components need it: the outer one
 *  declares before the first send (so the opening message is not racing its own
 *  context) and the inner one re-declares whenever the host pushes a change
 *  mid-conversation. One function means one description of the page rather than
 *  two that can disagree.
 *
 *  Both declarations are wholesale replacements, so calling it twice costs a
 *  round trip and changes nothing — the right trade against a turn arriving
 *  blind.
 *
 *  Failures are swallowed: a page that cannot describe itself must still be
 *  able to hold a conversation. A 422 in particular is the HOST author's bug
 *  (rows instead of a selection) and the server's message says how to fix it.
 */
export async function declarePage(
  client: { rest: { json: (path: string, init?: RequestInit) => Promise<unknown> } },
  link: HostLink,
  base: string,
): Promise<void> {
  const state = link.pageState()
  await Promise.allSettled([
    client.rest.json(`${base}/page-actions`, {
      method: 'PUT',
      body: JSON.stringify({ actions: link.actions() }),
    }),
    // Only if the host has actually spoken — pushing `{}` for a host that does
    // not use the state channel would declare the user's screen blank.
    state
      ? client.rest.json(`${base}/page-state`, {
          method: 'PUT',
          body: JSON.stringify({ state }),
        })
      : Promise.resolve(),
  ])
}

/** Where this principal's session lives.
 *
 *  A contact reaches `/api/contact/` and nothing else, so every by-session call
 *  has to pick. Derived in one place because the two paths diverging per
 *  call-site is what left contacts silently without page state.
 */
export function sessionBase(isContact: boolean, sid: string): string {
  const id = encodeURIComponent(sid)
  return isContact ? `/api/contact/sessions/${id}` : `/api/canopy-sessions/${id}`
}
