// The per-user GitHub App connection: status, disconnect, and the owner picker.
//
// The CONNECT action is deliberately not here. It is a full-page navigation to
// `/auth/github/start/`, not a fetch — an OAuth flow has to leave the SPA so
// GitHub can show its own consent screen, and it comes back as a redirect to
// `/settings?github=…`. A fetch would be blocked by CORS and, more to the
// point, would hide the consent screen the user is meant to read.
import { apiV2 } from './client.v2'
import { problemMessage } from './problem'
import type { components } from './generated'

export type GitHubConnectionOut = components['schemas']['GitHubConnectionOut']
export type GitHubInstallationOut = components['schemas']['GitHubInstallationOut']

export async function getGitHubConnection(): Promise<GitHubConnectionOut> {
  const res = await apiV2.GET('/api/tokens/github', {})
  // `res.error` narrows to `never` on a route that declares only success
  // responses, so branch on the response itself — the repo's standing rule for
  // openapi-fetch calls.
  if (!res.response.ok || !res.data) {
    throw new Error(problemMessage(res.error, 'Could not read the GitHub connection.'))
  }
  return res.data
}

export async function disconnectGitHub(): Promise<void> {
  const res = await apiV2.DELETE('/api/tokens/github', {})
  if (!res.response.ok) {
    throw new Error(problemMessage(res.error, 'Could not disconnect GitHub.'))
  }
}

/**
 * Where the caller has actually installed the app — the owner dropdown's only
 * source.
 *
 * A live read on every call, by design: the common path is that someone
 * *just* installed the app on an org in another tab and came back, so a cached
 * list would either omit the owner they added or offer one that no longer
 * works. A 409 here means "press Connect", not "retry".
 */
export async function listGitHubInstallations(): Promise<GitHubInstallationOut[]> {
  const res = await apiV2.GET('/api/tokens/github/installations', {})
  if (!res.response.ok || !res.data) {
    throw new Error(problemMessage(res.error, 'Could not list your GitHub installations.'))
  }
  // The `--immutable` codegen types an array response as a deeply-readonly
  // object that is not even iterable, so it cannot be spread or mapped. Cast
  // through `unknown`, the idiom already used by `listJoinableWorkspaces` in
  // api/workspaces.ts, rather than inventing a second approach here.
  return res.data as unknown as GitHubInstallationOut[]
}

/** The full-page navigation that starts the flow. Not a fetch — see the note above. */
export function gitHubConnectPath(): string {
  // BASE_URL carries the deployment's script prefix (`/canopy/` on labs), which
  // this must include: `/auth/github/start/` alone would land on a sibling
  // tenant, and the same prefix is baked into the callback registered on the
  // GitHub App itself.
  return `${import.meta.env.BASE_URL.replace(/\/$/, '')}/auth/github/start/`
}
