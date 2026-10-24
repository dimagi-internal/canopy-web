import { useEffect, useState, type ReactNode } from 'react'
import { Lock } from 'lucide-react'

import { getAgent } from '@/api/agents'
import { askToJoin, getSession, type ChatSessionDetail } from '@/api/chat'
import { Button } from 'canopy-ui/ui'

/**
 * A private (sensitive) conversation is opened on purpose. Whatever list it was
 * picked from (/ide, the Supervisor, Home, an agent's chats), the chat route shows
 * this first:
 *
 * - to one of its people: what it is, and [Open it] (the sealed history may then
 *   ask for the one-click passkey unlock);
 * - to someone who can see it exists but is not in it: that only its people can
 *   read it, and [Ask <starter> to add you], a request carrying nothing it said
 *   (`POST /{id}/join-requests`), which its people see in the conversation itself
 *   (`JoinRequests`). While it waits, this re-reads the conversation now and then,
 *   so an add turns into [Open it] (and a decline into "ask again") in place.
 *
 * An ordinary conversation passes straight through. The detail fetched to decide
 * is handed to the chat page (`takeGateFetch`), so a chat is still read once.
 */

const OPENED_KEY = (id: string) => `canopy.private-open.${id}`

function openedHere(id: string): boolean {
  try {
    return sessionStorage.getItem(OPENED_KEY(id)) === '1'
  } catch {
    return false
  }
}

function markOpened(id: string): void {
  try {
    sessionStorage.setItem(OPENED_KEY(id), '1')
  } catch {
    // A private window: the interstitial asks again next time, which is fine.
  }
}

const fetched = new Map<string, Promise<ChatSessionDetail>>()

/** The detail the gate already fetched for `id`, once; else null. */
export function takeGateFetch(id: string): Promise<ChatSessionDetail> | null {
  const p = fetched.get(id)
  fetched.delete(id)
  return p ?? null
}

type Decision = { kind: 'loading' } | { kind: 'open' } | { kind: 'ask'; session: ChatSessionDetail }

export function PrivateConversationGate({ sessionId, children }: { sessionId: string; children: ReactNode }) {
  const [decision, setDecision] = useState<Decision>({ kind: 'loading' })

  useEffect(() => {
    if (!sessionId) return
    let live = true
    setDecision({ kind: 'loading' })
    const p = getSession(sessionId)
    fetched.set(sessionId, p)
    p.then((s) => {
      if (!live) return
      setDecision(!s.sensitive || (s.you_are_in_it && openedHere(sessionId))
        ? { kind: 'open' } : { kind: 'ask', session: s })
    }).catch(() => {
      // Let the chat page say what went wrong, as it always has.
      if (live) setDecision({ kind: 'open' })
    })
    return () => {
      live = false
    }
  }, [sessionId])

  if (decision.kind === 'loading') return <p className="p-4 text-[13px] text-muted-foreground">Loading…</p>
  if (decision.kind === 'open') return <>{children}</>
  return (
    <PrivateInterstitial
      session={decision.session}
      onOpen={() => {
        markOpened(sessionId)
        // The chat page reads the detail afresh: the one fetched here was spent deciding.
        fetched.delete(sessionId)
        setDecision({ kind: 'open' })
      }}
    />
  )
}

/** How soon a waiting request first re-reads whether it was answered; each read that
 *  finds it still waiting doubles the wait, up to `ANSWER_POLL_MAX_MS`. */
export const ANSWER_POLL_MS = 10_000
export const ANSWER_POLL_MAX_MS = 5 * 60_000

export function PrivateInterstitial({ session, onOpen }: { session: ChatSessionDetail; onOpen: () => void }) {
  const agentName = useAgentName(session.agent_slug ?? null)
  const starter = (session.started_by_name ?? '').trim()
  const canAsk = Boolean(session.created_by)
  const [asked, setAsked] = useState(session.your_join_request ?? '')
  const [inIt, setInIt] = useState(Boolean(session.you_are_in_it))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [gone, setGone] = useState(false)

  // The requester is not on the conversation's socket (it is its people's alone),
  // so the answer is read, not pushed — less often the longer it waits, and not at all
  // once the conversation is out of reach (403/404: removed, or never theirs to see).
  useEffect(() => {
    if (inIt || gone || asked !== 'pending') return
    let live = true
    let wait = ANSWER_POLL_MS
    let timer = 0
    const poll = () => {
      getSession(session.id)
        .then((s) => {
          if (!live) return
          if (s.you_are_in_it) setInIt(true)
          else if ((s.your_join_request ?? '') !== 'pending') setAsked(s.your_join_request ?? '')
          else schedule()
        })
        .catch((e: unknown) => {
          if (!live) return
          const status = (e as { status?: number } | null)?.status
          if (status === 403 || status === 404) setGone(true)
          else schedule()
        })
    }
    const schedule = () => {
      timer = window.setTimeout(poll, wait)
      wait = Math.min(wait * 2, ANSWER_POLL_MAX_MS)
    }
    schedule()
    return () => {
      live = false
      window.clearTimeout(timer)
    }
  }, [session.id, inIt, gone, asked])

  const ask = async () => {
    setBusy(true)
    setError('')
    try {
      setAsked((await askToJoin(session.id)).status)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not send the request.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex h-full items-start justify-center p-6" data-testid="private-interstitial">
      <div className="w-full max-w-md rounded-lg border border-border bg-card p-4 text-[13px]">
        <p className="m-0 flex items-center gap-1.5 font-semibold text-foreground">
          <Lock className="h-3.5 w-3.5" aria-hidden />
          Private conversation
        </p>
        {inIt ? (
          <>
            <p className="mt-2 text-foreground-secondary" data-testid="private-interstitial-text">
              {!session.you_are_in_it && <>{starter || 'Its owner'} added you. </>}
              This is a private conversation{agentName ? ` with ${agentName}` : ''}
              {starter ? `, started by ${starter}` : ''}. Its messages are sealed to the people in it.
            </p>
            <Button size="sm" className="mt-3" onClick={onOpen} data-testid="private-open">
              Open it
            </Button>
          </>
        ) : (
          <>
            <p className="mt-2 text-foreground-secondary" data-testid="private-interstitial-text">
              This is a private conversation. Only the people in it can read it.
            </p>
            {gone ? (
              <p className="mt-3 text-muted-foreground" data-testid="private-gone">
                This conversation is no longer one you can ask to join.
              </p>
            ) : asked === 'pending' ? (
              <p className="mt-3 text-muted-foreground" data-testid="private-asked">
                Asked{starter ? ` ${starter}` : ''} to add you. You can open it once they do.
              </p>
            ) : canAsk ? (
              <>
                {asked === 'declined' && (
                  <p className="mt-2 text-muted-foreground">{starter || 'Its owner'} declined your last request.</p>
                )}
                <Button size="sm" className="mt-3" onClick={() => void ask()} disabled={busy}
                  data-testid="private-ask">
                  {starter ? `Ask ${starter} to add you` : 'Ask to be added'}
                </Button>
              </>
            ) : (
              <p className="mt-3 text-muted-foreground">Nobody in it can add you.</p>
            )}
            {error && <p className="mt-2 text-destructive">{error}</p>}
          </>
        )}
      </div>
    </div>
  )
}

function useAgentName(slug: string | null): string {
  const [name, setName] = useState('')
  useEffect(() => {
    if (!slug) return
    let live = true
    getAgent(slug)
      .then((a) => live && setName(a.name))
      .catch(() => live && setName(slug))
    return () => {
      live = false
    }
  }, [slug])
  return name
}
