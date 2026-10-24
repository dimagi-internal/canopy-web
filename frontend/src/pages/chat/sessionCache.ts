import type { Message } from 'canopy-ui/chat'

import type { ChatSession, ChatSessionDetail } from '@/api/chat'

/**
 * What this tab already knows about each chat it has seen, so opening one again
 * (or one the /ide list already showed) paints at once while the live copy loads
 * underneath: the socket's snapshot replaces the transcript, the REST read the
 * detail. Kept for the life of the tab, unbounded — a transcript tail is ~100 KB,
 * so even hundreds of opened chats stay a few tens of MB.
 *
 * A private (sensitive) conversation keeps nothing here but the fact that it is
 * private: its words and key stay with its own screen (`harness.sensitivity`),
 * and its page still opens through PrivateConversationGate's own read.
 */
interface Known {
  sensitive: boolean
  meta?: ChatSessionDetail
  messages?: Message[]
}

const known = new Map<string, Known>()

function remember(id: string, sensitive: boolean, keep: Omit<Known, 'sensitive'> = {}): void {
  known.set(id, sensitive ? { sensitive } : { ...known.get(id), ...keep, sensitive })
}

/** Rows a session list already read: enough to open each without asking again. */
export function rememberListed(rows: readonly Pick<ChatSession, 'id' | 'sensitive'>[]): void {
  for (const r of rows) remember(r.id, Boolean(r.sensitive))
}

/** The detail as just read. Its transcript is kept as messages, not twice. */
export function rememberMeta(meta: ChatSessionDetail): void {
  remember(meta.id, Boolean(meta.sensitive), { meta: { ...meta, messages: [] } })
}

/** The transcript as the socket last held it. */
export function rememberMessages(id: string, messages: Message[]): void {
  const k = known.get(id)
  if (k && !k.sensitive) known.set(id, { ...k, messages })
}

/** True only for a chat known NOT to be private: it may open without the gate's read. */
export function knownNotPrivate(id: string): boolean {
  return known.get(id)?.sensitive === false
}

export function cachedMeta(id: string): ChatSessionDetail | null {
  const k = known.get(id)
  return k && !k.sensitive ? k.meta ?? null : null
}

export function cachedMessages(id: string): Message[] | null {
  const k = known.get(id)
  return k && !k.sensitive && k.messages?.length ? k.messages : null
}

/** Tests only. */
export function forgetAll(): void {
  known.clear()
}
