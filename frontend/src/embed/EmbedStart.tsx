import { SendBox } from 'canopy-ui/chat'
import { type CanopyClient } from 'canopy-client'
import { X } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import type { HostLink } from './hostLink'
import { PRIVATE_LABEL, PrivateMark } from '@/components/chat/privateConversation'
import { conversationFacts, pageLabel } from './conversationLabels'
import { type EmbedAgent, type EarlierChat } from './types'
import { usePageState, AskingAbout } from './panelBits'
import { HEADER_ICON } from './HeaderButtons'

/**
 * The panel before there is a conversation.
 *
 * Deliberately not `ChatPanel` with an empty transcript: that component reads
 * its composer body off a server-side draft, and a draft belongs to a session
 * that does not exist yet. A plain composer is also the honest picture —
 * there is nothing to show above it.
 */
export function EmbedStart({
  agent,
  client,
  isContact,
  onStart,
  onOpen,
  onClose,
  onSwitch,
  onMakeDefault,
  link,
  initialBody,
  initialError,
  headerControls,
}: {
  agent: EmbedAgent
  client: CanopyClient
  isContact: boolean
  onStart: (text: string) => Promise<void>
  onOpen: (sessionId: string) => void
  onClose: () => void
  /** Back to the picker; absent when only one agent is on offer. */
  onSwitch?: () => void
  /** Make this agent the visitor's default; absent when it already is, or
   *  there is nothing to choose between. */
  onMakeDefault?: () => Promise<void>
  /** The page around the panel: what it is about, for the visitor to see. */
  link: HostLink
  /** A question the page asked that could not be sent: shown, not dropped. */
  initialBody?: string
  initialError?: string
  /** Panel controls drawn before × (expand). */
  headerControls?: React.ReactNode
}) {
  const { rows: history, putAway } = useEarlierChats(client, agent, isContact)
  const page = usePageState(link)
  const here = typeof page?.resource === 'string' ? page.resource : ''
  const pageName = pageLabel(page)
  const [body, setBody] = useState(initialBody ?? '')
  const [sending, setSending] = useState(false)
  const [error, setError] = useState<string | null>(initialError ?? null)

  const send = useCallback(() => {
    const text = body.trim()
    if (!text || sending) return
    setSending(true)
    setError(null)
    // SendBox empties its own box the moment Send is pressed; it puts a local
    // draft's body back only when that body CHANGES. So the body is emptied
    // here too, and set again on failure: without that it never changed, and a
    // failed start left the box blank with the words gone from the screen.
    setBody('')
    void onStart(text)
      .catch((e: unknown) => {
        // The text goes back in the box on failure. Losing what somebody typed
        // because a request failed is the worst possible response to it.
        setBody(text)
        setError(e instanceof Error && e.message ? e.message : 'Could not start the conversation.')
        setSending(false)
      })
  }, [body, sending, onStart])

  const [defaulting, setDefaulting] = useState(false)
  const makeDefault = onMakeDefault
    ? () => {
        setDefaulting(true)
        void onMakeDefault()
          .catch(() => setError('Could not make this your default.'))
          .finally(() => setDefaulting(false))
      }
    : undefined

  return (
    <div className="flex h-full flex-col">
      <header className="flex items-center justify-between border-b border-border px-3 py-2">
        <span className="flex min-w-0 items-center gap-2">
          {onSwitch ? (
            <button type="button" onClick={onSwitch} aria-label="Switch agent"
              title="Ask a different agent"
              className="truncate rounded px-1 text-[12px] text-muted-foreground hover:bg-muted hover:text-foreground">
              {agent.name} &#9662;
            </button>
          ) : (
            <span className="text-[12px] text-muted-foreground">{agent.name}</span>
          )}
          {makeDefault ? (
            <button type="button" onClick={makeDefault} disabled={defaulting}
              className="shrink-0 rounded px-1 text-[11px] text-primary hover:bg-muted disabled:opacity-50">
              Make this my default
            </button>
          ) : null}
        </span>
        <span className="flex shrink-0 items-center gap-1">
          {headerControls}
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className={HEADER_ICON}
          >
            <X className="h-3.5 w-3.5" aria-hidden />
          </button>
        </span>
      </header>
      <AskingAbout pageName={pageName} />

      {history.length > 0 ? (
        <div className="min-h-0 flex-1 overflow-y-auto p-3">
          <p className="px-1 pb-2 text-[12px] text-muted-foreground">
            Ask {agent.name} something new below, or pick up an earlier conversation:
          </p>
          <ul className="space-y-1">
            {history.map((s) => (
              <li
                key={s.id}
                className="flex items-stretch rounded-lg border border-border bg-card hover:bg-muted"
              >
                {/* Which conversation the row opens: private rows all read "Private
                    conversation", so a script (or a test) picks one by its id. */}
                <button
                  type="button"
                  onClick={() => onOpen(s.id)}
                  data-session-id={s.id}
                  className="min-w-0 flex-1 px-3 py-2 text-left"
                >
                  {/* What you first asked — the name you would recognise. The
                      session's title is canopy's, and means nothing here. */}
                  <span className="block truncate text-sm text-foreground">
                    {s.private ? (
                      <span className="inline-flex items-center gap-1" data-testid="private-title">
                        <PrivateMark />
                        {PRIVATE_LABEL}
                      </span>
                    ) : (
                      s.opening || 'Conversation'
                    )}
                  </span>
                  {/* What tells one apart from the next: when and where it
                      began. Metadata only — a private one keeps no words to be
                      named by, and canopy could not read them if it did. */}
                  <span className="block truncate text-[11px] text-muted-foreground" data-testid="conversation-facts">
                    {conversationFacts({ startedAt: s.startedAt, origin: s.origin }, here)}
                  </span>
                </button>
                {/* Archives it, so it is not offered here again. Nothing is
                    deleted — canopy's own chat list still has it. */}
                <button
                  type="button"
                  onClick={() => putAway(s.id)}
                  aria-label="Remove from this list"
                  title="Hide from this list (the conversation stays in canopy)"
                  className="shrink-0 px-3 text-muted-foreground hover:text-foreground"
                >
                  ×
                </button>
              </li>
            ))}
          </ul>
        </div>
      ) : (
        <div className="grid min-h-0 flex-1 place-items-center p-6">
          <p className="max-w-xs text-center text-sm text-muted-foreground">
            Ask {agent.name} about {pageName || 'this page'}.
          </p>
        </div>
      )}

      {/* The kit's own composer, not a hand-rolled one. This screen used to
          draw its own textarea and button, which is how it drifted: in light
          mode the box was filled with `bg-input` (a BORDER token in this set, so
          a mid-grey) and the Send button was a tiny `text-xs` pill at 50%
          opacity — the "button doesn't look good" the widget was reported for.
          Rendering SendBox with a local draft (the same adapter the contact
          path uses, since no server draft exists before the session does)
          makes this screen and the chat that follows it one component. */}
      <SendBox
        draft={contactDraft(body)}
        connected
        isStreaming={false}
        streamingMessageId={null}
        onUpdate={setBody}
        onSend={send}
        onStop={() => undefined}
        // Sending is the only thing that can happen here, so "blocked" is
        // exactly "starting" — and it says so where the button is.
        disabledReason={sending ? 'Starting…' : undefined}
        banner={
          error ? <p className="text-[12px] text-destructive">{error}</p> : undefined
        }
      />
    </div>
  )
}

/** How far back the start screen offers an earlier conversation. Older ones
 *  are still in canopy's own chat list; here they were a wall of stale rows
 *  between the visitor and the box they came to type in. */
const EARLIER_CHATS_DAYS = 7

/** Your earlier conversations with THIS agent on THIS site, newest first.
 *
 *  Canopy already scopes the list to the site (the `embed_app` it stamped at
 *  create, forced for a delegated token) and to the site's own agents, and a
 *  contact's list is their own conversations only — so the narrowing left here
 *  is to the agent this panel is for, to the last week, and to conversations
 *  nobody put away (`state=active`: the × archives one, and an archived chat
 *  must not come back on the next page load). A failed or odd response shows no
 *  history rather than an error: the panel's job is still to let you ask. */
function useEarlierChats(client: CanopyClient, agent: EmbedAgent, isContact: boolean) {
  const [rows, setRows] = useState<EarlierChat[]>([])
  useEffect(() => {
    let cancelled = false
    const url = isContact
      ? '/api/contact/sessions?state=active'
      : '/api/canopy-sessions/?state=active&limit=50'
    const since = Date.now() - EARLIER_CHATS_DAYS * 24 * 60 * 60 * 1000
    client.rest
      .json<unknown>(url)
      .then((data) => {
        if (cancelled || !Array.isArray(data)) return
        const mine = (data as Array<Record<string, unknown>>)
          // A chat with no question in it has nothing to be named by, and there
          // is nothing in it to pick up — leftovers of the empty-session-per-page
          // -load bug listed seven blank "Conversation" rows on one account.
          .filter(
            (r) =>
              r.agent_slug === agent.slug &&
              typeof r.id === 'string' &&
              // Someone else's private conversation is listed to a co-tenant by
              // what it is, so they may ask to join — but this list is YOUR
              // earlier conversations, and that one is not one of them.
              r.you_are_in_it !== false &&
              // A private conversation keeps no words to be named by; it is
              // listed as what it is.
              (r.sensitive === true || (typeof r.opening === 'string' && r.opening.trim() !== '')),
          )
          .map((r) => ({
            id: r.id as string,
            opening: typeof r.opening === 'string' ? r.opening : '',
            private: r.sensitive === true,
            at: String(r.last_activity_at ?? r.created_at ?? ''),
            startedAt: String(r.created_at ?? ''),
            origin: typeof r.origin_resource === 'string' ? r.origin_resource : '',
          }))
          .filter((r) => Date.parse(r.at) >= since)
          .sort((a, b) => b.at.localeCompare(a.at))
          .slice(0, 20)
        setRows(mine)
      })
      .catch(() => undefined)
    return () => {
      cancelled = true
    }
  }, [client, agent.slug, isContact])

  // Gone from the list at once; if the archive fails the row comes back, since
  // a row that silently reappears on the next load is the bug this fixes.
  const putAway = useCallback(
    (id: string) => {
      const removed = rows.find((r) => r.id === id)
      setRows((prev) => prev.filter((r) => r.id !== id))
      const url = isContact
        ? `/api/contact/sessions/${encodeURIComponent(id)}/archive`
        : `/api/canopy-sessions/${encodeURIComponent(id)}/archive`
      client.rest.json(url, { method: 'POST' }).catch(() => {
        if (!removed) return
        setRows((prev) =>
          [...prev, removed].sort((a, b) => b.at.localeCompare(a.at)),
        )
      })
    },
    [client, isContact, rows],
  )
  return { rows, putAway }
}

/** The local composer body, in the shape the shared panel already reads.
 *
 *  A member's draft is a server row that several people co-edit; a contact's
 *  cannot be, because every field that makes it multiplayer is keyed on a user
 *  id they do not have. Presenting theirs as a `Draft` keeps the shared kit
 *  from having to learn about a second principal for a difference that is
 *  entirely about where the text lives. The chat itself now gets this from
 *  the kit (`sendOverHttp`); the start screen still needs it, because no
 *  session — and so no socket — exists until its first send.
 */
function contactDraft(body: string) {
  return {
    id: 'local',
    slot: 'next' as const,
    status: 'open' as const,
    body,
    version: 0,
    last_editor: 0,
    last_edit_at: '',
  }
}
