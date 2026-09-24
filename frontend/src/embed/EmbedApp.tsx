import { ChatPanel, MenuPrompt, SendBox, useSessionSocket } from 'canopy-ui/chat'
import { createCanopyClient, type CanopyClient } from 'canopy-client'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { Markdown } from '@/components/Markdown'
import { menuBlocksComposer } from '@/pages/chatPageLogic'

import { buildPageContextBlock } from './pageContextBlock'
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
  /** An agent is settled on, but NOTHING has been created yet. The frame loads
   *  on every page view — the iframe's `src` is set when the chrome is built,
   *  not when the panel is opened — so anything created here would be created
   *  for people who never even opened the panel. */
  | { kind: 'ready'; agent: EmbedAgent }
  /** `firstMessage` is echoed into the transcript on mount. The opening message
   *  is sent over HTTP before this component exists, so nothing on the socket
   *  ever announced it: you typed, pressed send, and watched your own question
   *  disappear into an empty panel. */
  | { kind: 'chatting'; sessionId: string; firstMessage: string }

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

/** The same renderer canopy's own chat page injects, for the same reason.
 *
 *  `ChatPanel` defaults to `plainText` when this seam is left unwired, which is
 *  what the widget did — so an agent's reply arrived with its markdown as
 *  literal source: `**bold**`, `- ` before every bullet, unrendered fences
 *  around code. Same agent, same words, visibly worse than canopy-web, which
 *  is precisely the complaint. */
function renderMarkdown(text: string) {
  return <Markdown className="text-sm leading-relaxed">{text}</Markdown>
}

/** Tell canopy what this page can do and what it is showing.
 *
 *  Module-level, not a hook, because BOTH components need it: the outer one
 *  declares before the first send (so the opening message is not racing its own
 *  context) and the inner one re-declares whenever the host pushes a change
 *  mid-conversation. One function means one description of the page rather than
 *  two that can disagree.
 *
 *  Both declarations are wholesale replacements, so calling it twice costs a
 *  round trip and changes nothing — the right trade against a turn arriving
 *  blind.
 *
 *  Failures are swallowed: a page that cannot describe itself must still be
 *  able to hold a conversation. A 422 in particular is the HOST author's bug
 *  (rows instead of a selection) and the server's message says how to fix it.
 */
async function declarePage(
  client: { rest: { json: (path: string, init?: RequestInit) => Promise<unknown> } },
  link: HostLink,
  base: string,
): Promise<void> {
  const state = link.pageState()
  await Promise.allSettled([
    client.rest.json(`${base}/page-actions`, {
      method: 'PUT',
      body: JSON.stringify({ actions: link.actions() }),
    }),
    // Only if the host has actually spoken — pushing `{}` for a host that does
    // not use the state channel would declare the user's screen blank.
    state
      ? client.rest.json(`${base}/page-state`, {
          method: 'PUT',
          body: JSON.stringify({ state }),
        })
      : Promise.resolve(),
  ])
}

/** Where this principal's session lives.
 *
 *  A contact reaches `/api/contact/` and nothing else, so every by-session call
 *  has to pick. Derived in one place because the two paths diverging per
 *  call-site is what left contacts silently without page state.
 */
function sessionBase(isContact: boolean, sid: string): string {
  const id = encodeURIComponent(sid)
  return isContact ? `/api/contact/sessions/${id}` : `/api/canopy-sessions/${id}`
}

export function EmbedApp({ link, app }: Props) {
  const [phase, setPhase] = useState<Phase>({ kind: 'connecting' })
  const [init, setInit] = useState<HostInit | null>(null)
  const [principal, setPrincipal] = useState<Principal | null>(null)
  // Read through refs inside `startConversation`: both are set during the same
  // async mount flow that later calls it, and reading the state there is what
  // sent a contact down the tenant path once already.
  const principalRef = useRef<Principal | null>(null)
  principalRef.current = principal
  const initRef = useRef<HostInit | null>(null)
  initRef.current = init
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
          pendingContext.current = buildPageContextBlock(context)
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
          setPhase({ kind: 'ready', agent: preselected })
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


  /** Create the session and send the first message, in that order.
   *
   *  Deferred to the first send rather than done at mount. The frame loads on
   *  every page view, so creating on mount produced an empty session per page
   *  load — three of them appeared in a real session list within an hour of the
   *  widget being switched on, each with no runner, because no turn had ever
   *  been enqueued. A session IS a conversation; there is not one until
   *  somebody says something.
   *
   *  The first message goes over HTTP even on the user path, where later ones
   *  go over the socket. Creating, then connecting, then sending would have to
   *  wait for the socket to be up, and a first message that races the transport
   *  it depends on is the kind of thing that works until the day it does not.
   */
  const startConversation = useCallback(
    async (agent: EmbedAgent, text: string) => {
      const isContact = principalRef.current?.kind === 'contact'
      // The ASK first, the page context after. The runner names the emdash
      // task from the prompt's opening words, so leading with the context
      // produced tasks called `c-context-from-the-page-i-am-on-…` that nobody
      // could recognise as their own question.
      //
      // The DECLARED page state is what rides, read at send time so it is what
      // the screen says now rather than what it said at mount. `provideContext`
      // is the fallback for a host still on the old model.
      //
      // Until 2026-09-23 only the legacy snapshot rode here, so a host that had
      // moved to `setPageState` — as the guide tells them to — sent nothing with
      // the first message and the agent's only route to the page was
      // `current_page`. That is the path the guide itself says can be
      // unavailable: MCP servers connect asynchronously, and an agent whose MCP
      // tools arrive deferred will not go looking for one unprompted. Measured
      // on connect-labs: asked "what am I looking at?", the agent answered "I
      // can't see your screen" and made no tool call at all.
      const declared = link.pageState()
      const context = buildPageContextBlock((declared ?? {}) as Record<string, unknown>)
        ?? pendingContext.current
      const body = context ? `${text}\n\n${context}` : text
      pendingContext.current = null

      const created = isContact
        ? await client.rest.json<{ id: string }>('/api/contact/sessions', {
            method: 'POST',
            body: JSON.stringify({ agent_slug: agent.slug }),
          })
        : await client.rest.json<{ id: string }>(
            `/api/w/${encodeURIComponent(agent.workspace)}/canopy-sessions/`,
            {
              method: 'POST',
              body: JSON.stringify({
                agent_slug: agent.slug,
                title: '',
                // `embed_app` is NOT sent: canopy stamps it server-side from the
                // delegated token, and anything we sent under that key would be
                // discarded (apps/canopy_sessions/api.py).
                metadata: initRef.current?.metadata ?? {},
              }),
            },
          )

      // Declare the page BEFORE the turn is queued.
      //
      // Both declarations used to live in effects keyed on `sessionId`, which
      // React runs after the render that follows this send — so the observed
      // order in production was create, SEND, page-actions, page-state. The
      // first message of a conversation therefore enqueued a turn describing a
      // page that had not said anything yet, and whether the agent saw the
      // screen depended on whether a runner claimed the turn before two more
      // HTTP round-trips landed. It worked when the runner was slow.
      //
      // That is the worst shape a bug can take: it passes every time a human
      // tries it by hand, and the case it fails is the one that matters most —
      // "close the ones I'm looking at" as the OPENING message.
      //
      // Both principals declare. Contacts used to be skipped because neither
      // route was on `/api/contact/`; both are now, so an embedded agent learns
      // what its visitor is looking at whether or not that visitor happens to
      // hold a canopy account.
      const base = sessionBase(isContact, created.id)
      await declarePage(client, link, base)

      await client.rest.json(`${base}/send`, {
        method: 'POST',
        body: JSON.stringify({ text: body }),
      })

      // `text`, not `body`: the page-context block is for the agent to read,
      // not for the person who just typed the question to be shown back.
      setPhase({ kind: 'chatting', sessionId: created.id, firstMessage: text })
    },
    [client, link],
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
            onClick={() => setPhase({ kind: 'ready', agent })}
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

  if (phase.kind === 'ready') {
    return (
      <EmbedStart
        agent={phase.agent}
        onStart={(text) => startConversation(phase.agent, text)}
        onClose={() => link.requestClose()}
      />
    )
  }

  return (
    <EmbedChat
      sessionId={phase.sessionId}
      firstMessage={phase.firstMessage}
      client={client}
      link={link}
      contextPreamble={pendingContext}
      isContact={principal?.kind === 'contact'}
    />
  )
}

/**
 * The panel before there is a conversation.
 *
 * Deliberately not `ChatPanel` with an empty transcript: that component reads
 * its composer body off a server-side draft, and a draft belongs to a session
 * that does not exist yet. A plain composer is also the honest picture —
 * there is nothing to show above it.
 */
function EmbedStart({
  agent,
  onStart,
  onClose,
}: {
  agent: EmbedAgent
  onStart: (text: string) => Promise<void>
  onClose: () => void
}) {
  const [body, setBody] = useState('')
  const [sending, setSending] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const send = useCallback(() => {
    const text = body.trim()
    if (!text || sending) return
    setSending(true)
    setError(null)
    void onStart(text)
      .catch((e: unknown) => {
        // The text stays in the box on failure. Losing what somebody typed
        // because a request failed is the worst possible response to it.
        setError(e instanceof Error ? e.message : 'could not start the conversation')
        setSending(false)
      })
  }, [body, sending, onStart])

  return (
    <div className="flex h-full flex-col">
      <header className="flex items-center justify-between border-b border-border px-3 py-2">
        <span className="text-[12px] text-muted-foreground">{agent.name}</span>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close"
          className="rounded px-2 text-muted-foreground hover:text-foreground"
        >
          ×
        </button>
      </header>

      <div className="grid min-h-0 flex-1 place-items-center p-6">
        <p className="max-w-xs text-center text-sm text-muted-foreground">
          Ask {agent.name} about this page.
        </p>
      </div>

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
        currentUserId={0}
        holderIsPresent={false}
        isStreaming={false}
        streamingMessageId={null}
        onUpdate={setBody}
        onSend={send}
        onStop={() => undefined}
        onTakeOver={() => undefined}
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

function Centered({ children }: { children: React.ReactNode }) {
  return <div className="grid h-full place-items-center p-4 text-muted-foreground">{children}</div>
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

function EmbedChat({
  sessionId,
  firstMessage,
  client,
  link,
  contextPreamble,
  isContact = false,
}: {
  sessionId: string
  /** The opening message, already sent over HTTP. Echoed once on mount. */
  firstMessage: string
  client: CanopyClient
  link: HostLink
  contextPreamble: React.MutableRefObject<string | null>
  /** Whether this visitor is a contact rather than a signed-in canopy user.
   *
   *  ONE name for one fact, because it decides several things at once and they
   *  must not be able to disagree: which prefix every by-session call uses
   *  (`/api/contact/` is a contact's entire surface), and that the socket
   *  LISTENS — presence and the co-edited draft are keyed on a user id they do
   *  not have, so the composer is local and the send goes over HTTP (see
   *  apps/canopy_sessions/consumers.py).
   *
   *  It was `readOnlySocket`, named for the second consequence only. That is
   *  part of why a contact silently went without page state: the prop that knew
   *  the answer did not look like it was about routing. */
  isContact?: boolean
}) {
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
      if (frame.event === 'page.invalidate') {
        // Relayed straight out to the host page, which owns what "re-read"
        // means. The kit deliberately does not know this frame — it is canopy's
        // vocabulary, which is exactly what `onUnknownEvent` is for.
        const data = frame.data as { uri?: string } | undefined
        link.invalidate(String(data?.uri ?? ''))
        return
      }
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
          .json(`${sessionBase(isContact, sessionId)}/page-actions/${action.id}/result`, {
            method: 'POST',
            body: JSON.stringify(body),
          })
          .catch(() => undefined)
      })()
    },
    [client, link, sessionId, isContact],
  )

  // AG-UI on the wire. `session.page_action` and `page.invalidate` still reach
  // `onUnknownEvent` exactly as before: they ride AG-UI's CUSTOM event and
  // `fromAgui` unwraps them into the canopy frames this handler already reads.
  // (Page actions were silently dropped by the AG-UI projection until
  // 2026-09-18 — flipping this before that fix would have disabled every one.)
  // A contact's socket only LISTENS, so their message goes out over HTTP — the
  // kit's `sendOverHttp`, which keeps their draft local and raises the same
  // pending row and "waiting for a reply" a member's send does. The page
  // snapshot rides the first message, as it does for a member.
  const sendOverHttp = useCallback(
    (typed: string) => {
      const context = contextPreamble.current
      contextPreamble.current = null
      const body = context ? `${typed}\n\n${context}` : typed
      return client.rest.json(`${sessionBase(isContact, sessionId)}/send`, {
        method: 'POST',
        body: JSON.stringify({ text: body }),
      })
    },
    [client, sessionId, contextPreamble, isContact],
  )
  const socket = useSessionSocket({
    sessionId, wsUrl, onUnknownEvent, protocol: 'ag-ui',
    sendOverHttp: isContact ? sendOverHttp : undefined,
  })
  const menu = socket.state.menu ?? null

  // Tell canopy what this page can do, so the agent's tool list includes it.
  // Re-sent whenever the host's set changes — a page the user navigated to
  // offers different things, and a stale declaration is one the agent would
  // call into nothing.
  useEffect(() => {
    // The pre-send declaration already covered the opening message; this keeps
    // it true as the user navigates mid-conversation. Both paths go through
    // `declarePage`, so there is one description of the page rather than two
    // that can disagree.
    return link.onActionsChanged(() => void declarePage(client, link, sessionId))
  }, [client, link, sessionId])

  // Tell canopy what this page is SHOWING, and keep telling it.
  //
  // The other half of the page contract, and the one that was missing. Context
  // used to be read once when the frame initialised and pasted onto the first
  // message as prose, so a user who filtered the page after opening the chat
  // left the agent acting on a screen that no longer existed. Here the view is
  // pushed on every change and the agent re-reads it with `page_state`, so
  // "close the ones I'm looking at" is answerable on turn nine, not only turn
  // one.
  useEffect(() => {
    return link.onPageStateChanged(() => void declarePage(client, link, sessionId))
  }, [client, link, sessionId])

  // The opening message, into the transcript. It was sent over HTTP before this
  // component existed, so no frame announces it and it is not a server row
  // until the agent's transcript ships it back — which is seconds at best and
  // never if the runner is offline. `noteLocalSend` also raises the pending
  // bubble, so the panel says something is happening from the first paint.
  //
  // Once per session id: the reducer dedupes a repeat against recent identical
  // text, but re-running this on every render would still reset `awaitingReply`
  // after the reply had arrived.
  const echoed = useRef<string | null>(null)
  useEffect(() => {
    if (!firstMessage || echoed.current === sessionId) return
    echoed.current = sessionId
    socket.noteLocalSend(firstMessage)
  }, [firstMessage, sessionId, socket])

  // Answering the agent's dialog. Posted with the frame's own bearer token
  // rather than through `@/api/chat`, which authenticates with the app's
  // session cookie — the frame has none for this origin.
  const [answering, setAnswering] = useState(false)
  const [answerError, setAnswerError] = useState<string | null>(null)
  const onAnswerMenu = useCallback(
    (option: number | null, selections?: number[][] | null, texts?: (string | null)[] | null) => {
      setAnswering(true)
      setAnswerError(null)
      void client.rest
        .json<{ ok: boolean; reason: string }>(
          `/api/canopy-sessions/${encodeURIComponent(sessionId)}/answer-menu`,
          {
            method: 'POST',
            body: JSON.stringify({ option, selections, texts }),
          },
        )
        .then((r) => {
          // `ok:true` only means the tap was RELAYED. The runner's refusal
          // arrives later, on the menu itself (`answer_error`), which is why
          // this does not clear the dialog.
          if (!r.ok) setAnswerError(r.reason || 'the runner refused that answer')
        })
        .catch((e: unknown) =>
          setAnswerError(e instanceof Error ? e.message : 'could not send that answer'),
        )
        .finally(() => setAnswering(false))
    },
    [client, sessionId],
  )

  // Tell the runner a viewer is here (and stop when the panel closes), the same
  // attach/detach pair canopy's own chat page uses. Best-effort: never block
  // rendering on it.
  useEffect(() => {
    void client.rest.attach(sessionId).catch(() => undefined)
    return () => {
      void client.rest.detach(sessionId).catch(() => undefined)
    }
  }, [client, sessionId])

  const onSend = useCallback(() => {
    // The page snapshot rides the FIRST message rather than an opening turn of
    // its own. An automatic turn on open would claim a runner and produce an
    // agent reply before the user had said anything — and metadata alone never
    // reaches the agent, since canopy treats it as an opaque bag.
    // The ask leads here too. This path only runs for a message sent after the
    // session already exists, so it does not name an emdash task — but a reader
    // scrolling the transcript should still see the question before the JSON.
    const context = contextPreamble.current
    if (context) {
      contextPreamble.current = null
      socket.updateDraft(`${socket.state.active_draft?.body ?? ''}\n\n${context}`)
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
          state={socket.state}
          connected={socket.connected}
          currentUserId={socket.state.current_user_id}
          onSend={isContact ? socket.sendChat : onSend}
          onStop={isContact ? () => undefined : socket.stopChat}
          // `socket.awaitingReply` on BOTH paths now. It used to be `sending`
          // for a contact, which tracked the HTTP request and so cleared the
          // instant the POST returned — an indicator for ~200ms, then silence
          // for the actual wait. `noteLocalSend` raises it and only a stream
          // frame (or the server's settled status) lowers it.
          awaitingReply={socket.awaitingReply}
          onUpdateDraft={socket.updateDraft}
          onTakeOver={isContact ? () => undefined : socket.takeOverDraft}
          onDiscard={isContact ? () => socket.updateDraft('') : socket.discardDraft}
          draftPersistKey={sessionId}
          // A parsed dialog is drawn WHERE the composer would be, so a send
          // bounces as COMPOSER_NOT_VISIBLE. Same rule and same helper as
          // canopy's chat page — and deliberately only for a dialog with
          // options: an option-less marker is a guess, and a lock with no way
          // out is worse than a send that bounces loudly.
          disabledReason={
            menuBlocksComposer(menu) ? 'answer the question above to continue' : undefined
          }
          // The seams canopy's own chat page wires and this one did not. The
          // widget mounts the SAME panel, so the difference people saw was
          // never the component — it was six props.
          renderMarkdown={renderMarkdown}
          banner={
            // The agent stopped to ask something. Without this the widget went
            // silent and dead at exactly that moment: `activity` flips to
            // `blocked`, the panel correctly withdraws the "working" bubble,
            // and nothing took its place — so the one state with a button to
            // press rendered as an idle, finished conversation.
            //
            // Contacts are excluded: `/api/contact/` is their whole surface
            // and answer-menu is not on it, so a dialog they cannot answer is
            // shown as words rather than as buttons that would 403.
            menu && !isContact ? (
              <MenuPrompt
                menu={menu}
                busy={answering}
                error={answerError ?? undefined}
                onAnswer={onAnswerMenu}
              />
            ) : menu ? (
              <p className="text-[12px] text-warning">
                The agent is waiting on an answer from whoever is running it.
              </p>
            ) : undefined
          }
          emptyState={
            <p className="px-4 text-center text-[12px] text-muted-foreground">
              Nothing here yet.
            </p>
          }
        />
      </div>
    </div>
  )
}
