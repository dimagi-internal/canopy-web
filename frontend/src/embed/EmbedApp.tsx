import { ChatPanel, useSessionSocket } from 'canopy-ui/chat'
import { createCanopyClient, type CanopyClient } from '@canopy/client'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { buildContextPreamble } from './contextPreamble'
import { currentFrameBaseUrl } from './frameBase'
import type { HostInit, HostLink } from './hostLink'
import { resolvePrincipal, type Principal } from './principal'

/**
 * The chat surface inside the widget's iframe.
 *
 * Runs on canopy's own origin with its OWN bundled React, which is the whole
 * reason the widget works on a host we do not control: connect-labs is React
 * 18 with a global (non-module) build, and nothing here has to meet it.
 *
 * Everything this component cannot do for itself — mint a token, read the host
 * page, run an action on it — goes through `HostLink` over `postMessage`.
 */

type Phase =
  | { kind: 'connecting' }
  | { kind: 'failed'; message: string }
  | { kind: 'choosing'; agents: EmbedAgent[] }
  | { kind: 'chatting'; sessionId: string }

interface EmbedAgent {
  slug: string
  name: string
  description: string
  avatar_url: string
  workspace: string
}

interface Props {
  link: HostLink
  app: string
}

export function EmbedApp({ link, app }: Props) {
  const [phase, setPhase] = useState<Phase>({ kind: 'connecting' })
  const [init, setInit] = useState<HostInit | null>(null)
  const [principal, setPrincipal] = useState<Principal | null>(null)
  const clientRef = useRef<CanopyClient | null>(null)
  /** Captured once per session, at open — a snapshot, not a subscription (v2
   *  spec §8). Held so the first send can carry it to the agent. */
  const pendingContext = useRef<string | null>(null)

  // The client's one host-specific dependency is inverted into a callback, and
  // in here that callback is a postMessage round trip: the frame has no cookies
  // for the host's origin and so cannot mint anything itself.
  const client = useMemo(() => {
    if (clientRef.current) return clientRef.current
    const c = createCanopyClient({
      // Origin PLUS the deployment prefix. canopy under `/canopy` on the
      // shared labs host means a bare `/api/...` reaches the root tenant
      // (connect-labs) instead — see frameBase.ts.
      baseUrl: currentFrameBaseUrl(),
      fetchToken: async () => {
        const initial = init
        // The handshake's token is already in hand on the first call; asking
        // the host again immediately would mint a second one for nothing.
        if (initial?.token && !clientRef.current) {
          return { token: initial.token, expiresAt: '' }
        }
        return link.requestToken()
      },
      source: app,
    })
    clientRef.current = c
    return c
  }, [init, link, app])

  useEffect(() => {
    let cancelled = false

    link
      .waitForInit()
      .then(async (hostInit) => {
        if (cancelled) return
        setInit(hostInit)

        // Pull the page snapshot before anything else: it describes what the
        // user is looking at NOW, and the conversation may take a while to
        // start.
        try {
          const context = await link.requestContext()
          pendingContext.current = buildContextPreamble(context)
        } catch {
          // A host that cannot answer is a host that lends no context. Not
          // fatal — the agent simply starts without it.
          pendingContext.current = null
        }
        if (cancelled) return

        // WHO is behind this frame, before asking anything else: a user and a
        // contact reach entirely different surfaces. Discovered rather than
        // configured — see principal.ts.
        const who = await resolvePrincipal((path) => client.rest.json(path))
        if (cancelled) return
        setPrincipal(who)
        const agents = who.agents as EmbedAgent[]

        if (agents.length === 0) {
          setPhase({
            kind: 'failed',
            message:
              'No agent is available here yet. An administrator needs to allow ' +
              'one for this app, and you need to be a member of its workspace.',
          })
          return
        }

        // Skip the picker when there is nothing to pick — either the host named
        // an agent or only one is on offer.
        const preselected =
          (hostInit.agent && agents.find((a) => a.slug === hostInit.agent)) ??
          (agents.length === 1 ? agents[0] : null)

        if (preselected) {
          await openSession(preselected, hostInit, who)
          return
        }
        setPhase({ kind: 'choosing', agents })
      })
      .catch((error: unknown) => {
        if (cancelled) return
        setPhase({
          kind: 'failed',
          message: error instanceof Error ? error.message : 'could not start',
        })
      })

    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [link])

  const openSession = useCallback(
    // `who` is PASSED, not read from state. It is resolved and used in the same
    // async flow, and `setPrincipal` has not committed by then — so reading the
    // state here sent a contact down the tenant path, to
    // `/api/w/undefined/canopy-sessions/`. Caught by a test; invisible in the UI,
    // which would only have shown "could not start a conversation".
    async (agent: EmbedAgent, hostInit: HostInit, who: Principal | null) => {
      try {
        if (who?.kind === 'contact') {
          // A contact has no workspace to scope by and no membership to check;
          // the site's allowlist is the whole gate, and canopy applies it.
          const started = await client.rest.json<{ id: string }>('/api/contact/sessions', {
            method: 'POST',
            body: JSON.stringify({ agent_slug: agent.slug }),
          })
          setPhase({ kind: 'chatting', sessionId: started.id })
          return
        }
        // The TENANT-scoped path, with the workspace the picker's own row
        // named. The flat `/api/canopy-sessions/` resolves to the caller's
        // DEFAULT workspace, and `create_session` then 404s any agent that is
        // not in it — so an agent in a second workspace was offered by the
        // picker and refused on click. For a user in several workspaces with
        // no default, the flat route does not even get that far: it 422s with
        // "no unambiguous workspace". `/api/embed/agents` returns `workspace`
        // per row precisely so this call does not have to guess.
        const created = await client.rest.json<{ id: string }>(
          `/api/w/${encodeURIComponent(agent.workspace)}/canopy-sessions/`,
          {
            method: 'POST',
            body: JSON.stringify({
              agent_slug: agent.slug,
              title: '',
              // `embed_app` is NOT sent: canopy stamps it server-side from the
              // delegated token, and anything we sent under that key would be
              // discarded (apps/canopy_sessions/api.py).
              metadata: hostInit.metadata ?? {},
            }),
          },
        )
        setPhase({ kind: 'chatting', sessionId: created.id })
      } catch (error) {
        setPhase({
          kind: 'failed',
          message:
            error instanceof Error
              ? `could not start a conversation: ${error.message}`
              : 'could not start a conversation',
        })
      }
    },
    [client],
  )

  if (phase.kind === 'connecting') {
    return <Centered>Starting&hellip;</Centered>
  }

  if (phase.kind === 'failed') {
    return (
      <Centered>
        <p className="max-w-xs text-center text-foreground-secondary">{phase.message}</p>
      </Centered>
    )
  }

  if (phase.kind === 'choosing') {
    return (
      <div className="flex h-full flex-col gap-2 overflow-y-auto p-4">
        <h1 className="text-sm font-medium text-foreground">Who would you like to ask?</h1>
        {phase.agents.map((agent) => (
          <button
            key={agent.slug}
            type="button"
            onClick={() => init && void openSession(agent, init, principal)}
            className="rounded-lg border border-border bg-card p-3 text-left hover:bg-muted"
          >
            <span className="block text-sm text-foreground">{agent.name}</span>
            {agent.description ? (
              <span className="block text-[12px] text-muted-foreground">{agent.description}</span>
            ) : null}
          </button>
        ))}
      </div>
    )
  }

  return (
    <EmbedChat
      sessionId={phase.sessionId}
      client={client}
      link={link}
      contextPreamble={pendingContext}
      readOnlySocket={principal?.kind === 'contact'}
    />
  )
}

function Centered({ children }: { children: React.ReactNode }) {
  return <div className="grid h-full place-items-center p-4 text-muted-foreground">{children}</div>
}

/** The local composer body, in the shape the shared panel already reads.
 *
 *  A member's draft is a server row that several people co-edit; a contact's
 *  cannot be, because every field that makes it multiplayer is keyed on a user
 *  id they do not have. Presenting theirs as a `Draft` keeps the shared kit
 *  from having to learn about a second principal for a difference that is
 *  entirely about where the text lives.
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

function EmbedChat({
  sessionId,
  client,
  link,
  contextPreamble,
  readOnlySocket = false,
}: {
  sessionId: string
  client: CanopyClient
  link: HostLink
  contextPreamble: React.MutableRefObject<string | null>
  /** A contact's socket LISTENS. Presence and the co-edited draft are keyed on
   *  a user id they do not have, so the composer is local and the send goes
   *  over HTTP — see apps/canopy_sessions/consumers.py. */
  readOnlySocket?: boolean
}) {
  // Only used on the contact path. A member's draft is server-side and
  // co-edited; a contact's cannot be, so it lives here.
  const [localDraft, setLocalDraft] = useState('')
  const [sending, setSending] = useState(false)
  const wsUrl = useCallback(
    () => client.sessionSocketUrl(sessionId) ?? '',
    [client, sessionId],
  )
  // The agent asking this page to do something. Arrives on the session socket
  // as a DOORBELL only — the durable PageAction row is the mechanism, and the
  // result goes back over HTTP so it is an acknowledged write rather than a
  // second frame that could land nowhere.
  const onUnknownEvent = useCallback(
    (frame: { event: string; data?: unknown }) => {
      if (frame.event !== 'session.page_action') return
      const action = frame.data as { id: string; name: string; args: Record<string, unknown> }
      void (async () => {
        let body: { result?: unknown; error?: string }
        try {
          body = { result: await link.runAction(action.name, action.args) }
        } catch (error) {
          // A host refusal must reach the agent AS a refusal. Swallowing it
          // here would leave the action pending until it timed out, and the
          // agent would be told the page was closed when in fact it said no.
          body = { error: error instanceof Error ? error.message : 'the page refused the action' }
        }
        await client.rest
          .json(`/api/canopy-sessions/${sessionId}/page-actions/${action.id}/result`, {
            method: 'POST',
            body: JSON.stringify(body),
          })
          .catch(() => undefined)
      })()
    },
    [client, link, sessionId],
  )

  const socket = useSessionSocket({ sessionId, wsUrl, onUnknownEvent })

  // Tell canopy what this page can do, so the agent's tool list includes it.
  // Re-sent whenever the host's set changes — a page the user navigated to
  // offers different things, and a stale declaration is one the agent would
  // call into nothing.
  useEffect(() => {
    const declare = (actions: { name: string; description?: string; parameters?: unknown }[]) => {
      void client.rest
        .json(`/api/canopy-sessions/${sessionId}/page-actions`, {
          method: 'PUT',
          body: JSON.stringify({ actions }),
        })
        .catch(() => undefined)
    }
    declare(link.actions())
    return link.onActionsChanged(declare)
  }, [client, link, sessionId])

  // Tell the runner a viewer is here (and stop when the panel closes), the same
  // attach/detach pair canopy's own chat page uses. Best-effort: never block
  // rendering on it.
  useEffect(() => {
    void client.rest.attach(sessionId).catch(() => undefined)
    return () => {
      void client.rest.detach(sessionId).catch(() => undefined)
    }
  }, [client, sessionId])

  const onSendAsContact = useCallback(() => {
    const preamble = contextPreamble.current
    const body = preamble ? `${preamble}\n\n${localDraft}` : localDraft
    if (!body.trim() || sending) return
    contextPreamble.current = null
    setSending(true)
    void client.rest
      .json(`/api/contact/sessions/${encodeURIComponent(sessionId)}/send`, {
        method: 'POST',
        body: JSON.stringify({ text: body }),
      })
      .then(() => setLocalDraft(''))
      .finally(() => setSending(false))
  }, [client, sessionId, localDraft, sending, contextPreamble])

  const onSend = useCallback(() => {
    // The page snapshot rides the FIRST message rather than an opening turn of
    // its own. An automatic turn on open would claim a runner and produce an
    // agent reply before the user had said anything — and metadata alone never
    // reaches the agent, since canopy treats it as an opaque bag.
    const preamble = contextPreamble.current
    if (preamble) {
      contextPreamble.current = null
      socket.updateDraft(`${preamble}\n\n${socket.state.active_draft?.body ?? ''}`)
    }
    socket.sendChat()
  }, [socket, contextPreamble])

  return (
    <div className="flex h-full flex-col">
      <header className="flex items-center justify-between border-b border-border px-3 py-2">
        <span className="text-[12px] text-muted-foreground">Canopy</span>
        <button
          type="button"
          onClick={() => link.requestClose()}
          aria-label="Close"
          className="rounded px-2 text-muted-foreground hover:text-foreground"
        >
          ×
        </button>
      </header>
      <div className="min-h-0 flex-1">
        <ChatPanel
          state={
            readOnlySocket
              ? // The composer reads its body off the draft, and a contact has
                // no server-side one — so the local body is presented in the
                // shape the panel already understands rather than teaching the
                // shared kit about a second principal.
                { ...socket.state, active_draft: contactDraft(localDraft) }
              : socket.state
          }
          connected={socket.connected}
          currentUserId={socket.state.current_user_id}
          onSend={readOnlySocket ? onSendAsContact : onSend}
          onStop={readOnlySocket ? () => undefined : socket.stopChat}
          awaitingReply={readOnlySocket ? sending : socket.awaitingReply}
          onUpdateDraft={readOnlySocket ? setLocalDraft : socket.updateDraft}
          onTakeOver={readOnlySocket ? () => undefined : socket.takeOverDraft}
          onDiscard={readOnlySocket ? () => setLocalDraft('') : socket.discardDraft}
          draftPersistKey={sessionId}
        />
      </div>
    </div>
  )
}
