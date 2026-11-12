/** Guests: workspaces the caller is ONLY a guest of (docs/architecture/access.md § Guests).
 *  Kept apart from ./workspaces so a page's mock of that module never has to know about it. */
import { apiV2 } from './client.v2'
import type { components } from './generated'

export type GuestWorkspaceOut = components['schemas']['GuestWorkspaceOut']

/** Each workspace the caller is only a guest of, with the apps it opens to them
 *  (`scope.apps`: app key -> app role). The shell shows a guest those apps and nothing else. */
export async function listGuestWorkspaces(): Promise<GuestWorkspaceOut[]> {
  const { data } = await apiV2.GET('/api/workspaces/guest-of')
  return (data as unknown as GuestWorkspaceOut[]) ?? []
}

/** The app keys a guest scope opens, in a stable order. */
export function guestAppKeys(g: GuestWorkspaceOut | undefined): string[] {
  const apps = (g?.scope as { apps?: Record<string, string> } | undefined)?.apps ?? {}
  return Object.keys(apps).sort()
}
