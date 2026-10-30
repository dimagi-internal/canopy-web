/**
 * The zero-knowledge vault flows (canopy-web#1550), composed from the crypto
 * module (`./zk`), the passkey PRF ceremony (`./prf`) and the REST calls
 * (`@/api/vault`). Every secret here — the vault key VK, a person's private
 * scalar, a recovery secret, a decrypted value — lives only in local variables
 * of one call and is never sent anywhere; what goes to canopy-web is
 * ciphertext, wraps and seals. Zero-knowledge is the agent vault's only mode:
 * canopy-web holds no value it can read.
 */
export * from './userKey'
export * from './userKeySetup'
export * from './agentVault'
export * from './agentKeyOps'
export * from './endorsements'
export * from './workspaceVault'
export type { PublicJwk } from './zk'
