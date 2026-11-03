import { X } from 'lucide-react'
import { type ComponentProps } from 'react'
import { SessionMemoryToggles } from '@/components/chat/SessionMemoryToggles'
import { ContactMemory } from './ContactMemory'
import type { HostLink } from './hostLink'
import { ExpandButton, HEADER_ICON, SnapButton } from './HeaderButtons'
import type { EmbedAgent } from './types'

export function EmbedChatHeader({ agent, onBack, sessionId, sensitive, memorySource, contactMemory, link, snappable, expandable }: {
  agent: EmbedAgent
  onBack: () => void
  sessionId: string
  /** Unknown (null) until the session is read: memory's switches wait for it. */
  sensitive: boolean | null
  memorySource: ComponentProps<typeof SessionMemoryToggles>['source'] | null
  contactMemory: ComponentProps<typeof ContactMemory>['hcp'] | null
  link: HostLink
  snappable: boolean
  expandable: boolean
}) {
  return (
    <header className="flex items-center justify-between border-b border-border px-3 py-2">
      <span className="flex min-w-0 items-center gap-1">
        <button
          type="button"
          onClick={onBack}
          aria-label="All conversations"
          title="All conversations"
          className="rounded px-1 text-muted-foreground hover:text-foreground"
        >
          ‹
        </button>
        <span className="truncate text-[12px] text-muted-foreground">{agent.name}</span>
      </span>
      <span className="flex shrink-0 items-center gap-1">
        {/* Memory is off in a private conversation (the server refuses it too), so
            its switches are not offered there at all. */}
        {memorySource && sensitive === false && <SessionMemoryToggles sessionId={sessionId} source={memorySource} />}
        {contactMemory && (
          <ContactMemory hcp={contactMemory} sessionId={sessionId} agentName={agent.name} />
        )}
        {/* The ‹ alone was the only way to a new question, and nobody read a
            bare chevron as "new chat". Same destination — the start screen,
            whose composer starts a fresh conversation — said in words. */}
        <button
          type="button"
          onClick={onBack}
          className="rounded px-2 py-0.5 text-[12px] text-muted-foreground hover:bg-muted hover:text-foreground"
        >
          New chat
        </button>
        <SnapButton link={link} snappable={snappable} />
        <ExpandButton link={link} expandable={expandable} />
        <button
          type="button"
          onClick={() => link.requestClose()}
          aria-label="Close"
          className={HEADER_ICON}
        >
          <X className="h-3.5 w-3.5" aria-hidden />
        </button>
      </span>
    </header>
  )
}
