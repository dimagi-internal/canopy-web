// Canopy Vault Phase 2 (canopy-web#1550): the zero-knowledge vault's REST calls.
// Every body here is ciphertext, a wrap or a seal: there is no way back to
// custodial storage. Its own module so the vault UI can be tested without
// dragging in ./agents.
import { apiV2 } from './client.v2'
import type { components } from './generated'
import { ApiError, unwrap } from './problem'

type Schemas = components['schemas']
export type VaultKeysetOut = Schemas['VaultKeysetOut']
export type ZkStateOut = Schemas['ZkStateOut']
export type ZkWrapOut = Schemas['ZkWrapOut']
export type ZkTargetOut = Schemas['ZkTargetOut']
export type ZkWrapIn = Schemas['ZkWrapIn']

/** A refused call; `.code` is the server's machine-readable `error` member.
 *  The shared `ApiError` (./problem) under its old name. */
export { ApiError as VaultApiError }

function need<T>(res: { data?: unknown; error?: unknown; response?: Response }, fallback: string): T {
  return unwrap(res, fallback) as T
}

/** The caller's keyset. A 404 (`no_vault_keyset`) is "not set up yet": its
 *  body still carries user_id, prf_input and passkeys, returned as exists:false. */
export async function getKeyset(): Promise<VaultKeysetOut> {
  const res = await apiV2.GET('/api/vault/keyset')
  const err = (res as { error?: unknown }).error as (Partial<VaultKeysetOut> & { error?: string }) | undefined
  if (res.response?.status === 404 && err?.error === 'no_vault_keyset' && err.user_id) {
    return { user_id: err.user_id, prf_input: err.prf_input ?? '', exists: false, public_jwk: null, seals: [],
             passkeys: err.passkeys ?? [] } as VaultKeysetOut
  }
  return need<VaultKeysetOut>(res, 'Could not load your vault key')
}

export async function putKeyset(body: { public_jwk: Record<string, unknown>; seals: Record<string, unknown>[]; replace?: boolean }): Promise<VaultKeysetOut> {
  return need<VaultKeysetOut>(await apiV2.PUT('/api/vault/keyset', { body: { replace: false, ...body } as never }), 'Could not save your vault key')
}

export async function getZkState(slug: string, values = false): Promise<ZkStateOut> {
  return need<ZkStateOut>(
    await apiV2.GET('/api/agents/{slug}/vault/zk', { params: { path: { slug }, query: { values } } }),
    'Could not load the zero-knowledge state',
  )
}

/** `expect`: compare-and-swap — hex SHA-256 of the stored value being replaced
 *  ("" = none stored); a mismatch refuses the whole PUT (409 zk_value_changed). */
type Value = { name: string; value_zk: string; expect?: string | null }

export async function enableZk(slug: string, body: { kv: number; wraps: ZkWrapIn[]; values: Value[] }): Promise<ZkStateOut> {
  return need<ZkStateOut>(await apiV2.POST('/api/agents/{slug}/vault/zk/enable', { params: { path: { slug } }, body: body as never }),
    'Could not turn on zero-knowledge')
}

export async function putZkValues(slug: string, body: { kv: number; values: Value[] }) {
  return need<{ kv: number; names: string[] }>(await apiV2.PUT('/api/agents/{slug}/vault/zk/values', { params: { path: { slug } }, body }),
    'Could not save the value')
}

export async function addZkWraps(slug: string, body: { kv: number; wraps: ZkWrapIn[] }): Promise<ZkStateOut> {
  return need<ZkStateOut>(await apiV2.POST('/api/agents/{slug}/vault/zk/wraps', { params: { path: { slug } }, body: body as never }),
    'Could not grant access')
}

export async function rotateZk(slug: string, body: { new_kv: number; wraps: ZkWrapIn[]; values: Value[] }): Promise<ZkStateOut> {
  return need<ZkStateOut>(await apiV2.POST('/api/agents/{slug}/vault/zk/rotate', { params: { path: { slug } }, body: body as never }),
    'Could not rotate the vault key')
}

// ---- give an agent a secret (spec 2026-10-19-give-an-agent-a-secret-design.md) ----

export type ZkRecipientsOut = Schemas['ZkRecipientsOut']
export type ZkRecipientOut = Schemas['ZkRecipientOut']
export type ZkEndorsementOut = Schemas['ZkEndorsementOut']
export type ZkEndorsementIn = Schemas['ZkEndorsementIn']
export type SecretRequestOut = Schemas['SecretRequestOut']

/** Who a sealed value is for, and who vouched for each — canopy-web's CLAIM;
 *  verify the endorsements before sealing (`./vault/trust`). */
export async function getZkRecipients(slug: string, secretRequest?: string): Promise<ZkRecipientsOut> {
  return need<ZkRecipientsOut>(await apiV2.GET('/api/agents/{slug}/vault/zk/recipients', {
    params: { path: { slug }, query: secretRequest ? { secret_request: secretRequest } : {} } }),
  'Could not load who can read this agent')
}

export async function addZkEndorsements(slug: string, endorsements: ZkEndorsementIn[]): Promise<{ stored: number }> {
  return need<{ stored: number }>(await apiV2.POST('/api/agents/{slug}/vault/zk/endorsements', {
    params: { path: { slug } }, body: { endorsements } }), 'Could not confirm who can read this agent')
}

export async function getSecretRequest(slug: string, id: string): Promise<SecretRequestOut> {
  return need<SecretRequestOut>(await apiV2.GET('/api/agents/{slug}/secret-requests/{request_id}', {
    params: { path: { slug, request_id: id } } }), 'Could not load the request')
}

export async function fulfilSecretRequest(slug: string, id: string, body: { kv: number; value_zk: string }):
    Promise<SecretRequestOut> {
  return need<SecretRequestOut>(await apiV2.PUT('/api/agents/{slug}/secret-requests/{request_id}/value', {
    params: { path: { slug, request_id: id } }, body }), 'Could not store the value')
}
