/**
 * The key a chat started in the embedded panel carries (`SessionCreateIn.conversation_key`).
 */
import type { CanopyClient } from 'canopy-client'
import { offerForNewChat, type MyVaultKey } from '@/vault/sealedChat'

/** `{conversation_key}` for a new chat: a key pair made in this frame, wrapped to the
 *  visitor's own vault key (`GET …/my-vault-key`, which the frame's token may read) —
 *  its public half only, so nothing is unlocked: a key pinned in this frame is used, one
 *  never seen here is trusted on first use and checked at the first unlock here
 *  (`offerForNewChat`). Nothing when they have no vault key (404). A key that CHANGED
 *  since this frame pinned it THROWS: the conversation is not started, and the panel
 *  says why. */
export async function offeredKey(client: CanopyClient, workspace: string):
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
  return { conversation_key: await offerForNewChat(mine) }
}
