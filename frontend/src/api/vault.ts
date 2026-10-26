// Canopy Vault Phase 2 (canopy-web#1550): the zero-knowledge vault's REST calls.
// Every body here is ciphertext, a wrap or a seal: canopy-web holds no agent
// value it can read. Its own module so the vault UI can be tested without
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
             passkeys: err.passkeys ?? [], test_key: false } as VaultKeysetOut
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

/** The agent's requests still waiting for an answer (admins only) — names and reasons, never a value. */
export async function listSecretRequests(slug: string): Promise<SecretRequestOut[]> {
  return need<SecretRequestOut[]>(await apiV2.GET('/api/agents/{slug}/secret-requests', {
    params: { path: { slug } } }), 'Could not load what the agent asked for')
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

// ---- vault membership (spec 2026-10-22-vault-access-follows-canopy-access-design.md) ----

export type VaultMembersOut = Schemas['VaultMembersOut']
export type VaultMemberOut = Schemas['VaultMemberOut']

export async function getVaultMembers(slug: string): Promise<VaultMembersOut> {
  return need<VaultMembersOut>(await apiV2.GET('/api/agents/{slug}/vault/members', { params: { path: { slug } } }),
    'Could not load who can open this vault')
}

export async function addVaultMember(slug: string, email: string, role: 'admin' | 'member'): Promise<VaultMembersOut> {
  return need<VaultMembersOut>(await apiV2.POST('/api/agents/{slug}/vault/members', {
    params: { path: { slug } }, body: { email, role } }), 'Could not add them to the vault')
}

export async function removeVaultMember(slug: string, userId: number): Promise<VaultMembersOut> {
  return need<VaultMembersOut>(await apiV2.DELETE('/api/agents/{slug}/vault/members/{user_id}', {
    params: { path: { slug, user_id: userId } } }), 'Could not remove them from the vault')
}

export async function setVaultAgentLink(slug: string, linked: boolean): Promise<VaultMembersOut> {
  return need<VaultMembersOut>(await apiV2.PUT('/api/agents/{slug}/vault/agent-link', {
    params: { path: { slug } }, body: { linked, role: 'member' } }), 'Could not change the vault’s link to this agent')
}

// ---- workspace vaults (spec §4): any number per workspace, layered under an agent's own ----

export type WorkspaceVaultOut = Schemas['WorkspaceVaultOut']

export async function listWorkspaceVaults(slug: string): Promise<WorkspaceVaultOut[]> {
  return need<WorkspaceVaultOut[]>(await apiV2.GET('/api/workspaces/{slug}/vaults', { params: { path: { slug } } }),
    'Could not load this workspace’s vaults')
}

export async function createWorkspaceVault(slug: string, name: string): Promise<WorkspaceVaultOut> {
  return need<WorkspaceVaultOut>(await apiV2.POST('/api/workspaces/{slug}/vaults', {
    params: { path: { slug } }, body: { name } }), 'Could not make the vault')
}

/** One workspace vault: its members (and why), what it serves, its secrets' names. */
export async function getWorkspaceVault(vaultId: string): Promise<WorkspaceVaultOut> {
  return need<WorkspaceVaultOut>(await apiV2.GET('/api/vaults/{vault_id}', { params: { path: { vault_id: vaultId } } }),
    'Could not load the vault')
}

export async function deleteWorkspaceVaultSecret(vaultId: string, name: string): Promise<WorkspaceVaultOut> {
  return need<WorkspaceVaultOut>(await apiV2.DELETE('/api/vaults/{vault_id}/secrets/{name}', {
    params: { path: { vault_id: vaultId, name } } }), 'Could not delete the secret')
}

export async function attachWorkspaceVault(vaultId: string, to: { agent?: string; workspace?: string; runner?: string }):
    Promise<WorkspaceVaultOut> {
  return need<WorkspaceVaultOut>(await apiV2.POST('/api/vaults/{vault_id}/attachments', {
    params: { path: { vault_id: vaultId } }, body: to }), 'Could not attach the vault')
}

export async function detachWorkspaceVault(vaultId: string, attachmentId: number): Promise<WorkspaceVaultOut> {
  return need<WorkspaceVaultOut>(await apiV2.DELETE('/api/vaults/{vault_id}/attachments/{attachment_id}', {
    params: { path: { vault_id: vaultId, attachment_id: attachmentId } } }), 'Could not detach the vault')
}

export async function addWorkspaceVaultMember(vaultId: string, email: string, role: 'admin' | 'member'):
    Promise<WorkspaceVaultOut> {
  return need<WorkspaceVaultOut>(await apiV2.POST('/api/vaults/{vault_id}/members', {
    params: { path: { vault_id: vaultId } }, body: { email, role } }), 'Could not add them to the vault')
}

// A workspace vault's zero-knowledge routes take the agent vault's shapes, with the
// vault's binding id as `agent_id` — so the same operations drive either.

export async function getWorkspaceVaultZk(vaultId: string, values = false): Promise<ZkStateOut> {
  return need<ZkStateOut>(await apiV2.GET('/api/vaults/{vault_id}/zk', {
    params: { path: { vault_id: vaultId }, query: { values } } }), 'Could not load the vault’s key state')
}

export async function enableWorkspaceVaultZk(vaultId: string, body: { kv: number; wraps: ZkWrapIn[]; values: Value[] }):
    Promise<ZkStateOut> {
  return need<ZkStateOut>(await apiV2.POST('/api/vaults/{vault_id}/zk/enable', {
    params: { path: { vault_id: vaultId } }, body: body as never }), 'Could not make the vault’s key')
}

export async function rotateWorkspaceVaultZk(vaultId: string,
    body: { new_kv: number; wraps: ZkWrapIn[]; values: Value[] }): Promise<ZkStateOut> {
  return need<ZkStateOut>(await apiV2.POST('/api/vaults/{vault_id}/zk/rotate', {
    params: { path: { vault_id: vaultId } }, body: body as never }), 'Could not rotate the vault’s key')
}

export async function putWorkspaceVaultValues(vaultId: string, body: { kv: number; values: Value[] }) {
  return need<{ kv: number; names: string[] }>(await apiV2.PUT('/api/vaults/{vault_id}/zk/values', {
    params: { path: { vault_id: vaultId } }, body: body as never }), 'Could not save the value')
}

export async function addWorkspaceVaultWraps(vaultId: string, body: { kv: number; wraps: ZkWrapIn[] }):
    Promise<ZkStateOut> {
  return need<ZkStateOut>(await apiV2.POST('/api/vaults/{vault_id}/zk/wraps', {
    params: { path: { vault_id: vaultId } }, body: body as never }), 'Could not grant the vault’s key')
}

export async function removeWorkspaceVaultMember(vaultId: string, userId: number): Promise<WorkspaceVaultOut> {
  return need<WorkspaceVaultOut>(await apiV2.DELETE('/api/vaults/{vault_id}/members/{user_id}', {
    params: { path: { vault_id: vaultId, user_id: userId } } }), 'Could not remove them from the vault')
}

export type VaultSetupOut = Schemas['VaultSetupOut']

/** Must this person set up a passkey and vault key before anything else? */
export async function getVaultSetup(): Promise<VaultSetupOut> {
  return need<VaultSetupOut>(await apiV2.GET('/api/vault/setup'), 'Could not check your vault key')
}

export type VaultKeeperPinsIn = Schemas['VaultKeeperPinsIn']
export type VaultKeeperPinsOut = Schemas['VaultKeeperPinsOut']

/** Store the keepers' record of an agent's vault (owner/admin, after a grant). */
export async function putAgentZkPins(slug: string, body: VaultKeeperPinsIn): Promise<VaultKeeperPinsOut> {
  return need<VaultKeeperPinsOut>(await apiV2.PUT('/api/agents/{slug}/vault/zk/pins', {
    params: { path: { slug } }, body }), 'Could not record the granted keys')
}

/** Store the keepers' record of a workspace vault (owner/admin, after a grant). */
export async function putWorkspaceVaultPins(vaultId: string, body: VaultKeeperPinsIn): Promise<VaultKeeperPinsOut> {
  return need<VaultKeeperPinsOut>(await apiV2.PUT('/api/vaults/{vault_id}/zk/pins', {
    params: { path: { vault_id: vaultId } }, body }), 'Could not record the granted keys')
}

// ---- an agent login's TEST vault key (apps/agents/credentials/login_test_key.py) -------------

export type TestVaultKeyOut = Schemas['TestVaultKeyOut']

export async function getTestVaultKey(slug: string): Promise<TestVaultKeyOut> {
  const res = await apiV2.GET('/api/agents/{slug}/test-vault-key', { params: { path: { slug } } })
  return need<TestVaultKeyOut>(res, 'getTestVaultKey')
}

/** Turning it on needs a passkey step-up, which `apiV2` prompts for; off never does. */
export async function setTestVaultKey(slug: string, enabled: boolean): Promise<TestVaultKeyOut> {
  const res = await apiV2.PUT('/api/agents/{slug}/test-vault-key', {
    params: { path: { slug } },
    body: { enabled },
  })
  return need<TestVaultKeyOut>(res, 'setTestVaultKey')
}
