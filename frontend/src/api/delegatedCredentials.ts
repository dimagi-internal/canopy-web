/**
 * Credentials from your own accounts that you delegated to agents (your GitHub token,
 * given to ACE). Sealed so only the machines that run the agent can open them: you see
 * which agent holds each, which machines can open it, what the provider said, and you
 * withdraw it here. Never the value.
 */
import { apiV2 } from './client.v2'
import type { components } from './generated'

export type MyDelegatedCredential = components['schemas']['MyDelegatedCredentialOut']

// Branch on `res.response.ok`, never `res.error` (see tokens.ts).
export async function listMyDelegatedCredentials(): Promise<MyDelegatedCredential[] | null> {
  const res = await apiV2.GET('/api/me/delegated-credentials/')
  if (!res.response.ok || !res.data) return null
  return res.data as unknown as MyDelegatedCredential[]
}

export async function withdrawMyDelegatedCredential(id: number): Promise<boolean> {
  const res = await apiV2.DELETE('/api/me/delegated-credentials/{credential_id}', {
    params: { path: { credential_id: id } },
  })
  return res.response.ok
}
