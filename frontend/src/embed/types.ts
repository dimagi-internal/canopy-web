import type { ChatSessionDetail } from '@/api/chat'

export type Phase =
  | { kind: 'connecting' }
  | { kind: 'failed'; message: string }
  | { kind: 'choosing'; agents: EmbedAgent[] }
  /** An agent is settled on, but NOTHING has been created yet. The frame loads
   *  on every page view — the iframe's `src` is set when the chrome is built,
   *  not when the panel is opened — so anything created here would be created
   *  for people who never even opened the panel. */
  | { kind: 'ready'; agent: EmbedAgent }
  /** `firstMessage` is echoed into the transcript on mount. The opening message
   *  is sent over HTTP before this component exists, so nothing on the socket
   *  ever announced it: you typed, pressed send, and watched your own question
   *  disappear into an empty panel. `firstMessageClientId` is the id that HTTP
   *  send carried, so the echo can be matched against the server's queued-turn
   *  projection instead of rendering twice — see `QueuedRows.hideClientIds`. */
  | { kind: 'chatting'; sessionId: string; firstMessage: string; firstMessageClientId: string; agent: EmbedAgent }
  /** The page asked to open a conversation by id (`widget.openSession`), and the
   *  panel is reading it AS THE VISITOR to learn whether they may. `agent` is the
   *  one to go back to. */
  | { kind: 'opening'; sessionId: string; agent: EmbedAgent | null }
  /** It is private and the visitor is not in it: canopy's own interstitial, which
   *  offers to ask its people to add them. */
  | { kind: 'private'; session: ChatSessionDetail; agent: EmbedAgent }
  /** canopy would not give it to them (or it is with an agent this site does not
   *  offer). Said once, with nothing about it. */
  | { kind: 'unavailable'; agent: EmbedAgent | null; reason?: string }

export interface EmbedAgent {
  slug: string
  name: string
  description: string
  avatar_url: string
  workspace: string
  default?: boolean
  preferred?: boolean
}

export interface EarlierChat {
  id: string
  /** The start of the first message (`SessionOut.opening`), trimmed by canopy. */
  opening: string
  /** A private (sensitive) conversation: listed by what it is, never by its words. */
  private?: boolean
  /** Newest activity, which orders the list. */
  at: string
  /** When it began (`created_at`) — metadata, shown to tell rows apart. */
  startedAt: string
  /** The page it began on (`origin_resource`), "" when none named one. */
  origin: string
}
