/**
 * The key a chat started in the embedded panel carries (`SessionCreateIn.conversation_key`).
 */
import type { CanopyClient } from 'canopy-client'
import { unlockLocally } from '@/vault/localUnlock'
import type { UnlockPrompt } from '@/vault/operations'
import { offerForNewChat, type MyVaultKey } from '@/vault/sealedChat'

/** `{conversation_key}` for a new chat: a key pair made in this frame, wrapped to the
 *  visitor's own vault key (`GET …/my-vault-key`, which the frame's token may read) —
 *  only once this frame has confirmed that key is theirs (`offerForNewChat`: pinned here,
 *  or derived now by opening it from the keyset that comes with it). Nothing when they
 *  have no vault key (404). A key this frame cannot confirm, or one that changed, THROWS:
 *  the conversation is not started, and canopy never makes the key in its place. */
export async function offeredKey(client: CanopyClient, workspace: string, prompt: UnlockPrompt):
    Promise<{ conversation_key?: unknown }> {
  let mine: MyVaultKey
  try {
    mine = await client.rest.json<MyVaultKey>(
      `/api/w/${encodeURIComponent(workspace)}/canopy-sessions/my-vault-key`)
  } catch (e) {
    if ((e as { status?: number })?.status === 404) return {}
    throw e
  }
  // An answer that names no key says the same as a 404.
  if (!mine || typeof mine.jkt !== 'string' || !mine.public_jwk) return {}
  const ks = mine.keyset
  const derive = ks ? () => unlockLocally(ks, prompt) : undefined
  return { conversation_key: await offerForNewChat(mine, derive) }
}
