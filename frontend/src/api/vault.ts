// Canopy Vault Phase 2 (canopy-web#1550): the zero-knowledge vault's REST calls.
// Every body here is ciphertext, a wrap or a seal — except `disableZk`, which
// sends the plaintexts back for custodial storage (the one way out). Its own
// module so the vault UI can be tested without dragging in ./agents.
import { apiV2 } from './client.v2'
import type { components } from './generated'
import { problemMessage } from './problem'

type Schemas = components['schemas']
export type VaultKeysetOut = Schemas['VaultKeysetOut']
export type ZkStateOut = Schemas['ZkStateOut']
export type ZkWrapOut = Schemas['ZkWrapOut']
export type ZkTargetOut = Schemas['ZkTargetOut']
export type ZkWrapIn = Schemas['ZkWrapIn']

/** A refused call, with the server's machine-readable `error` code. */
export class VaultApiError extends Error {
  code: string
  constructor(message: string, code: string) {
    super(message)
    this.code = code
  }
}

function need<T>(res: { data?: unknown; error?: unknown }, fallback: string): T {
  if (res.error !== undefined || res.data === undefined) {
    const code = (res.error as { error?: unknown } | undefined)?.error
    throw new VaultApiError(problemMessage(res.error, fallback), typeof code === 'string' ? code : '')
  }
  return res.data as T
}

export async function getKeyset(): Promise<VaultKeysetOut> {
  return need<VaultKeysetOut>(await apiV2.GET('/api/vault/keyset'), 'Could not load your vault key')
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

type Value = { name: string; value_zk: string }

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

export async function disableZk(slug: string, body: { values: { name: string; plaintext: string }[] }): Promise<ZkStateOut> {
  return need<ZkStateOut>(await apiV2.POST('/api/agents/{slug}/vault/zk/disable', { params: { path: { slug } }, body: body as never }),
    'Could not turn off zero-knowledge')
}
