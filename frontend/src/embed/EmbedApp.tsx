import { newClientId } from 'canopy-ui/chat'
import { createCanopyClient, type CanopyClient } from 'canopy-client'
import { X } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { offeredKey } from './offeredKey'
import { buildPageContextBlock } from './pageContextBlock'
import { pickInitialAgent } from './pickAgent'
import { currentFrameBaseUrl } from './frameBase'
import type { HostInit, HostLink } from './hostLink'
import { resolvePrincipal, type Principal } from './principal'
import { agentLabel } from './conversationLabels'
import type { ChatSessionDetail, JoinRequest } from '@/api/chat'
import { PrivateInterstitial, type InterstitialApi } from '@/components/chat/PrivateConversationGate'
import { formDataSafeFetch } from './panelFiles'
import { createPageGrant, type PageGrant } from './pageGrant'
import { type Phase, type EmbedAgent } from './types'
import { declarePage, sessionBase } from './session'
import { Centered } from './panelBits'
import { HEADER_ICON, ExpandButton, SnapButton } from './HeaderButtons'
import { PageGrantConfirm } from './PageGrantConfirm'
import { EmbedStart } from './EmbedStart'
import { EmbedChat } from './EmbedChat'

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

interface Props {
  link: HostLink
  app: string
}

/** Wait before trying a page's question a second time. */
const ASK_RETRY_MS = 1500

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
  /** The agents this site offers this visitor — the only ones a conversation
   *  opened by id may be with. */
  const agentsRef = useRef<EmbedAgent[]>([])
  /** How many agents are on offer — whether switching means anything. */
  const [agentCount, setAgentCount] = useState(0)
  const clientRef = useRef<CanopyClient | null>(null)
  /** This tab's act-as-you permission: asked for only once the person acts here
   *  (pageGrant.ts), never on load. */
  const grantRef = useRef<PageGrant | null>(null)
  if (grantRef.current === null) {
    grantRef.current = createPageGrant({
      client: () => clientRef.current as CanopyClient,
      isContact: () => principalRef.current?.kind === 'contact',
    })
  }
  const pageGrant = grantRef.current
  useEffect(() => () => pageGrant.dispose(), [pageGrant])
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
          return { token: initial.token, expiresAt: initial.expiresAt ?? '' }
        }
        // A grant only once the person has acted in this panel (pageGrant.ts).
        return link.requestToken({ grant: grantRef.current?.wanted() ?? false })
      },
      source: app,
      // A file upload is multipart: the browser writes its own Content-Type.
      fetchImpl: formDataSafeFetch,
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
        // Named once, here, for every place the panel says who it is: an agent
        // registered with its slug as its name reads as a word, not an id.
        const agents = (who.agents as EmbedAgent[]).map((a) => ({ ...a, name: agentLabel(a) }))
        agentsRef.current = agents
        setAgentCount(agents.length)

        if (agents.length === 0) {
          // A contact is offered no agent that acts on this site (they cannot give
          // it an act-as-you permission): when that leaves nothing, no panel at all.
          if (who.kind === 'contact') link.reportUnavailable()
          setPhase({
            kind: 'failed',
            message:
              'No agent is available here yet. An administrator needs to allow ' +
              'one for this app, and you need to be a member of its workspace.',
          })
          return
        }

        // Skip the picker when there is nothing to pick: canopy's default for
        // this visitor, else the agent the host named, else the only one.
        const preselected = pickInitialAgent(agents, hostInit.agent)

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
      //
      // Since 2026-09-26 a DECLARED page state no longer rides in the message at
      // all: it is declared below, before the turn is queued, and canopy puts a
      // pointer to it ("they are looking at labs-marketplace://orgs, 21 visible;
      // read it with current_page") into the session's context through the
      // caller envelope + the canopy plugin's UserPromptSubmit hook — outside
      // the person's words. Pasting it here put a JSON dump under every first
      // message in the transcript. Only a host still on `provideContext` (no
      // declared state, so nothing for the envelope to point at) keeps the
      // appended block.
      // The person is sending: from now on this tab's mints ask its host for an
      // act-as-you permission, and the conversation is created (and so stamped)
      // with the token that carries it.
      await pageGrant.ensure()
      // The permission is usable only once the person confirms it with their
      // passkey, and the panel asks for that only when the turn needs it (canopy
      // pushes `host_grant.requested`): the send never waits.
      const declared = link.pageState()
      const context = declared ? null : pendingContext.current
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
                // The conversation's key, made in this frame: if it is (a sensitive
                // site's) or becomes private, canopy never sees its private half.
                ...await offeredKey(client, agent.workspace),
                // `embed_app` is NOT sent: canopy stamps it server-side from the
                // delegated token, and anything we sent under that key would be
                // discarded (apps/canopy_sessions/api_lifecycle.py).
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

      // Minted here rather than in `EmbedChat`'s echo effect: the id has to
      // ride the SAME POST that carries the text, so the server's queued-turn
      // projection and the optimistic echo (`noteLocalSend`, below) agree on
      // what to call this send.
      const clientId = newClientId()
      await client.rest.json(`${base}/send`, {
        method: 'POST',
        body: JSON.stringify({ text: body, client_id: clientId }),
      })

      // `text`, not `body`: the page-context block is for the agent to read,
      // not for the person who just typed the question to be shown back.
      setPhase({ kind: 'chatting', sessionId: created.id, firstMessage: text, firstMessageClientId: clientId, agent })
    },
    [client, link, pageGrant],
  )

  // A conversation the PAGE asked to open (`widget.openSession`) — a pointer it
  // kept. Read as the visitor, through the frame's own token, so canopy's session
  // ACL (and its narrowing to this site's agents) decides; the page is told only
  // what the panel then opens (`reportSession`), never why it could not.
  //
  // EVERY way into a conversation comes through here — the page's
  // `openSession`, a row of the start screen's list, and a chat whose socket
  // canopy turned away — so none of them hands an id straight to a socket that
  // may refuse it. A list row used to: it went to the chat directly, and a
  // conversation the visitor could not read showed a chat that said
  // "Reconnecting…" over "Nothing here yet", forever.
  const [openRequest, setOpenRequest] = useState<{ id: string; refused?: string } | null>(null)
  useEffect(() => link.onOpenSession((id) => setOpenRequest({ id })), [link])
  const settled = phase.kind !== 'connecting' && phase.kind !== 'failed'
  useEffect(() => {
    if (openRequest === null || !settled) return
    const { id, refused } = openRequest
    setOpenRequest(null)
    const back = 'agent' in phase ? phase.agent : null
    if (refused === undefined && phase.kind === 'chatting' && phase.sessionId === id) return
    const isContact = principalRef.current?.kind === 'contact'
    setPhase({ kind: 'opening', sessionId: id, agent: back })
    void client.rest
      .json<ChatSessionDetail>(sessionBase(isContact, id))
      .then((detail) => {
        const agent = agentsRef.current.find((a) => a.slug === detail.agent_slug)
        if (!agent) {
          setPhase({ kind: 'unavailable', agent: back })
        } else if (detail.sensitive && detail.you_are_in_it === false) {
          setPhase({ kind: 'private', session: detail, agent })
        } else if (refused !== undefined) {
          // canopy would list it but not open it live (e.g. a private one whose
          // key this person was never given): said, with canopy's reason, once.
          setPhase({ kind: 'unavailable', agent, reason: refused })
        } else {
          setPhase({ kind: 'chatting', sessionId: id, firstMessage: '', firstMessageClientId: '', agent })
        }
      })
      .catch(() => setPhase({ kind: 'unavailable', agent: back, reason: refused }))
  }, [openRequest, settled, phase, client])

  // No conversation open: the page is told so. (An open one is reported by
  // `EmbedChat`, once canopy has said where it began.)
  useEffect(() => {
    if (phase.kind === 'ready' || phase.kind === 'choosing' || phase.kind === 'private' ||
        phase.kind === 'unavailable') {
      link.reportSession(null)
    }
  }, [phase.kind, link])

  const interstitialApi = useMemo<InterstitialApi>(() => ({
    getSession: (id) => client.rest.json<ChatSessionDetail>(sessionBase(false, id)),
    askToJoin: (id) => client.rest.json<JoinRequest>(`${sessionBase(false, id)}/join-requests`, { method: 'POST' }),
  }), [client])

  // A question the PAGE asked for the visitor (`widget.ask`, e.g. "Explain this
  // answer"). It starts a new conversation as soon as one can start — at once
  // if the agent is known, after the visitor picks one if there are several —
  // and is the visitor's first message, shown to them exactly as sent.
  const [asked, setAsked] = useState<string | null>(null)
  const [unsent, setUnsent] = useState<{ text: string; error: string; n: number } | null>(null)
  useEffect(() => link.onAsk(setAsked), [link])

  // Esc collapses an expanded panel. Keys typed in here never reach the host
  // page, so the frame listens for its own. Something that handles Esc first
  // (a View shown full screen, a dialog) calls preventDefault and is left be.
  useEffect(() => {
    const onKeydown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || event.defaultPrevented || !link.isExpanded()) return
      link.requestExpand(false)
    }
    document.addEventListener('keydown', onKeydown)
    return () => document.removeEventListener('keydown', onKeydown)
  }, [link])
  const expandable = init?.expandable === true
  const snappable = init?.snappable === true
  useEffect(() => {
    if (asked === null) return
    if (phase.kind !== 'ready' && phase.kind !== 'chatting') return
    const text = asked
    const agent = phase.agent
    setAsked(null)
    // Never lost. A start that fails is tried once more (a question asked as
    // the page loads can meet a transient refusal); one that fails again
    // leaves the question in the composer with the reason, for the visitor to
    // send, instead of dropping back to an empty start screen.
    void startConversation(agent, text)
      .catch(() => new Promise((r) => setTimeout(r, ASK_RETRY_MS)).then(() => startConversation(agent, text)))
      .catch((e: unknown) => {
        setUnsent((u) => ({
          text,
          error: e instanceof Error ? e.message : 'could not start the conversation',
          n: (u?.n ?? 0) + 1,
        }))
        setPhase({ kind: 'ready', agent })
      })
  }, [asked, phase, startConversation])

  /** The visitor makes this agent the one every panel opens on for them. */
  const makeDefault = useCallback(async (agent: EmbedAgent) => {
    await client.rest.json('/api/embed/default-agent', {
      method: 'PUT',
      body: JSON.stringify({ agent: agent.slug }),
    })
    agentsRef.current = agentsRef.current.map((a) => ({ ...a, preferred: a.slug === agent.slug }))
    setPhase((p) => (p.kind === 'ready' && p.agent.slug === agent.slug
      ? { kind: 'ready', agent: { ...p.agent, preferred: true } }
      : p))
  }, [client])

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
            <span className="block text-sm text-foreground">
              {agent.name}
              {agent.preferred ? <span className="ml-2 text-[11px] text-muted-foreground">your default</span> : null}
            </span>
            {agent.description ? (
              <span className="block text-[12px] text-muted-foreground">{agent.description}</span>
            ) : null}
          </button>
        ))}
      </div>
    )
  }

  if (phase.kind === 'opening') {
    return <Centered>Opening the conversation&hellip;</Centered>
  }

  if (phase.kind === 'unavailable' || phase.kind === 'private') {
    const agent = phase.agent
    const toStart = () =>
      setPhase(agent ? { kind: 'ready', agent } : { kind: 'choosing', agents: agentsRef.current })
    return (
      <div className="flex h-full flex-col">
        <header className="flex items-center justify-between border-b border-border px-3 py-2">
          <button type="button" onClick={toStart} aria-label="All conversations"
            className="rounded px-1 text-[12px] text-muted-foreground hover:text-foreground">
            ‹ {agent?.name ?? 'Back'}
          </button>
          <button type="button" onClick={() => link.requestClose()} aria-label="Close" className={HEADER_ICON}>
            <X className="h-3.5 w-3.5" aria-hidden />
          </button>
        </header>
        <div className="min-h-0 flex-1 overflow-y-auto">
          {phase.kind === 'private' ? (
            <PrivateInterstitial
              session={phase.session}
              api={interstitialApi}
              agentName={phase.agent.name}
              onOpen={() => setPhase({
                kind: 'chatting', sessionId: phase.session.id, firstMessage: '', firstMessageClientId: '',
                agent: phase.agent,
              })}
            />
          ) : (
            <div className="space-y-2 p-6 text-center" data-testid="session-unavailable">
              <p className="text-sm text-foreground">This conversation isn&rsquo;t available here.</p>
              <p className="text-[12px] text-muted-foreground">
                {phase.reason
                  ? phase.reason.charAt(0).toUpperCase() + phase.reason.slice(1)
                  : 'It may be someone else\u2019s, with an agent this page does not offer, or no longer there.'}
              </p>
              <button type="button" onClick={toStart}
                className="rounded px-2 py-1 text-[12px] text-primary hover:bg-muted">
                Start a new conversation
              </button>
            </div>
          )}
        </div>
      </div>
    )
  }

  const confirmPrompt = principal?.kind === 'contact' ? null : (
    <PageGrantConfirm pageGrant={pageGrant}
      agentName={phase.kind === 'ready' || phase.kind === 'chatting' ? phase.agent.name : 'The agent'} />
  )

  if (phase.kind === 'ready') {
    return (
      <>
      {confirmPrompt}
      <EmbedStart
        // Remounted with an unsent question, so the composer starts from it.
        key={unsent ? `unsent-${unsent.n}` : 'start'}
        initialBody={unsent?.text}
        initialError={unsent?.error}
        agent={phase.agent}
        client={client}
        isContact={principal?.kind === 'contact'}
        onStart={(text) => startConversation(phase.agent, text)}
        onOpen={(sessionId) => setOpenRequest({ id: sessionId })}
        onClose={() => link.requestClose()}
        // Several on offer: the name opens the picker, and a person (not a
        // contact, who has no preference) may make this one their default.
        onSwitch={agentCount > 1 ? () => setPhase({ kind: 'choosing', agents: agentsRef.current }) : undefined}
        onMakeDefault={agentCount > 1 && principal?.kind !== 'contact' && !phase.agent.preferred
          ? () => makeDefault(phase.agent) : undefined}
        link={link}
        headerControls={
          <>
            <SnapButton link={link} snappable={snappable} />
            <ExpandButton link={link} expandable={expandable} />
          </>
        }
      />
      </>
    )
  }

  return (
    <>
    {confirmPrompt}
    <EmbedChat
      key={phase.sessionId}
      sessionId={phase.sessionId}
      firstMessage={phase.firstMessage}
      firstMessageClientId={phase.firstMessageClientId}
      agent={phase.agent}
      onBack={() => setPhase({ kind: 'ready', agent: phase.agent })}
      onRefused={(reason) => setOpenRequest({ id: phase.sessionId, refused: reason })}
      client={client}
      link={link}
      contextPreamble={pendingContext}
      isContact={principal?.kind === 'contact'}
      expandable={expandable}
      snappable={snappable}
      pageGrant={pageGrant}
    />
    </>
  )
}

