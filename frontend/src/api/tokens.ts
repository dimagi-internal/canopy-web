/**
 * Personal Access Tokens. The raw value comes back from mint() exactly once —
 * PersonalTokenCreatedOut is the only shape that carries it, and the server
 * stores a hash. The UI must therefore show it immediately and never imply it
 * can be recovered.
 */
import { apiV2 } from './client.v2'
import { problemMessage } from './problem'
import type { components } from './generated'

export type PersonalToken = components['schemas']['PersonalTokenOut']
export type MintedToken = components['schemas']['PersonalTokenCreatedOut']

// Every function here branches on `res.response.ok`, NEVER on `res.error`.
// All three token endpoints declare ONLY a success response in the OpenAPI
// schema (200 / 201 / 204), so `res.error` narrows to `never` and
// `if (res.error)` fails tsc. `frontend/src/api/workspaces.ts:32-36` documents
// this trap and every function in that file follows the same rule. `res.error`
// is still safe to READ for a message — just not to branch on.

export async function listTokens(): Promise<PersonalToken[]> {
  const res = await apiV2.GET('/api/tokens/')
  if (!res.response.ok || !res.data) return []
  // The generated response type is a readonly array; workspaces.ts's
  // listMembers hits the same shape and casts through unknown rather than
  // spreading, so this follows that convention.
  return res.data as unknown as PersonalToken[]
}

export async function mintToken(
  label: string,
  ttlDays: number | null,
): Promise<MintedToken | { error: string }> {
  const res = await apiV2.POST('/api/tokens/', {
    body: { label, ttl_days: ttlDays },
  })
  if (!res.response.ok || !res.data) {
    return { error: problemMessage(res.error, 'Could not mint a token.') }
  }
  return res.data
}

export async function revokeToken(id: number): Promise<boolean> {
  const res = await apiV2.DELETE('/api/tokens/{pk}/', {
    params: { path: { pk: id } },
  })
  return res.response.ok
}
