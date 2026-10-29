import {
  ChatPanel, MenuPrompt, SendBox, appResourceUpdate, createAppResourceUpdates, newClientId,
  useSessionSocket,
  type AppHost, type AppResourceUpdates, type AppViewResource, type CallToolResult,
} from 'canopy-ui/chat'
import { createCanopyClient, type CanopyClient } from 'canopy-client'
import { ChevronDown, Lock, Maximize2, Minimize2, Pin, X } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { Markdown } from '@/components/Markdown'
import { SessionMemoryToggles } from '@/components/chat/SessionMemoryToggles'
import { SealedConversationBanner, sealedAppHost } from '@/components/chat/SealedConversationBanner'
import { PRIVATE_DETAILS, PRIVATE_LINE } from '@/components/chat/PrivateConversationNote'
import { SessionAudienceLine, type SessionAudience } from '@/components/SessionAudienceLine'
import { unlockLocally } from '@/vault/localUnlock'
import type { SealedKey } from '@/vault/sealedChat'
import { offeredKey } from './offeredKey'
import { usePersonTrust } from '@/vault/PersonTrustPrompt'
import { useUnlockPrompt } from '@/vault/UnlockPrompt'
import { useSealedConversation } from '@/vault/useSealedConversation'
import { menuBlocksComposer } from '@/pages/chatPageLogic'

import { buildPageContextBlock } from './pageContextBlock'
import { pickInitialAgent } from './pickAgent'
import { currentFrameBaseUrl } from './frameBase'
import type { HostInit, HostLink } from './hostLink'
import { resolvePrincipal, type Principal } from './principal'
import { ContactMemory } from './ContactMemory'
import { contactHcp, embedMemorySource } from './sessionMemory'
import { PRIVATE_LABEL, PrivateMark } from '@/components/chat/privateConversation'
import { agentLabel, conversationFacts, pageLabel } from './conversationLabels'
import { JoinRequests, joinRequestsFrame, type JoinRequestActions } from '@/components/chat/JoinRequests'
import { addPerson, type PeopleApi } from '@/components/chat/ChatPeople'
import type { ChatSessionDetail, JoinRequest } from '@/api/chat'
import { PrivateInterstitial, type InterstitialApi } from '@/components/chat/PrivateConversationGate'
import { PRIVATE_NO_KEY, useAttachments, type PrivateFiles } from '@/pages/chat/useAttachments'
import { sealedAttachmentHost, useSealedAttachments } from '@/components/chat/SealedAttachments'
import { formDataSafeFetch, panelAttachmentApi, panelSealedFilesApi } from './panelFiles'
import { createPageGrant, isNoGrant, untilLabel, type PageGrant } from './pageGrant'
import { openInBrowser, PASSKEY_IN_BROWSER, passkeysWorkHere, useWhileAway } from '@/auth/passkeyHere'

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

interface EmbedAgent {
  slug: string
  name: string
  description: string
  avatar_url: string
  workspace: string
  default?: boolean
  preferred?: boolean
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
  return (
    <Markdown className="text-sm leading-relaxed" linksInNewTab>
      {text}
    </Markdown>
  )
}

/** Whether the host has the panel expanded, as it last said — the host owns
 *  the panel's box, so the frame follows its answer rather than its own guess. */
function useExpanded(link: HostLink): boolean {
  const [expanded, setExpanded] = useState(() => link.isExpanded())
  useEffect(() => link.onDisplayChanged(setExpanded), [link])
  return expanded
}

/** One size and hover for every icon control in the panel header, so the pin,
 *  expand and close read as one row rather than three glyphs of different
 *  widths. `aria-pressed` paints the on state of a toggle. */
const HEADER_ICON =
  'inline-flex h-6 w-6 items-center justify-center rounded text-muted-foreground transition-colors ' +
  'hover:bg-muted hover:text-foreground aria-pressed:bg-muted aria-pressed:text-foreground'

/** Expand / collapse, beside ×. Drawn only when the host allows it (`init`). */
function ExpandButton({ link, expandable }: { link: HostLink; expandable: boolean }) {
  const expanded = useExpanded(link)
  if (!expandable) return null
  const label = expanded ? 'Collapse panel' : 'Expand panel'
  return (
    <button
      type="button"
      onClick={() => link.requestExpand(!expanded)}
      aria-label={label}
      aria-pressed={expanded}
      title={expanded ? 'Collapse (Esc)' : 'Expand'}
      className={HEADER_ICON}
    >
      {expanded ? (
        <Minimize2 className="h-3.5 w-3.5" aria-hidden />
      ) : (
        <Maximize2 className="h-3.5 w-3.5" aria-hidden />
      )}
    </button>
  )
}

function useSnapped(link: HostLink): boolean {
  const [snapped, setSnapped] = useState(() => link.isSnapped())
  useEffect(() => link.onSnapChanged(setSnapped), [link])
  return snapped
}

/** Snap a docked rail into the page (the page makes room) or float it over the
 *  page again. Drawn only when the host allows it, and not while expanded —
 *  an expanded panel covers the page whichever way it is set. */
function SnapButton({ link, snappable }: { link: HostLink; snappable: boolean }) {
  const snapped = useSnapped(link)
  const expanded = useExpanded(link)
  if (!snappable || expanded) return null
  const label = snapped ? 'Float over the page' : 'Snap into the page'
  return (
    <button
      type="button"
      onClick={() => link.requestSnap(!snapped)}
      aria-label={label}
      aria-pressed={snapped}
      title={label}
      className={HEADER_ICON}
    >
      {/* A pin that is pressed in, not a struck-through one: the crossed-out
          glyph read as "broken" sitting in the header of a pinned panel. */}
      <Pin className={snapped ? 'h-3.5 w-3.5 fill-current' : 'h-3.5 w-3.5'} aria-hidden />
    </button>
  )
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
      // passkey, and the panel asks for that only if the turn needs it: the send
      // never waits, and the tab watches for the turn's ask.
      pageGrant.watch()
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

interface EarlierChat {
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

/** What the host page last declared it is showing, kept current. */
function usePageState(link: HostLink): Record<string, unknown> | null {
  const [state, setState] = useState(() => link.pageState())
  useEffect(() => link.onPageStateChanged(setState), [link])
  return state
}

/** One line under the header saying which page the agent is told about — the
 *  page the host declares, which canopy hands the agent with every turn. Nothing
 *  when the host declares none, since then the agent is told nothing either. */
function AskingAbout({ pageName }: { pageName: string }) {
  if (!pageName) return null
  return (
    <p className="truncate border-b border-border px-3 py-1 text-[11px] text-muted-foreground" data-testid="asking-about">
      Asking about <span className="text-foreground">{pageName}</span>
    </p>
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
  firstMessageClientId,
  agent,
  onBack,
  onRefused,
  client,
  link,
  contextPreamble,
  isContact = false,
  expandable = false,
  snappable = false,
  pageGrant,
}: {
  sessionId: string
  /** The opening message, already sent over HTTP. Echoed once on mount. Empty
   *  when an earlier conversation is reopened — its history comes from the
   *  socket's snapshot. */
  firstMessage: string
  /** The `client_id` that same HTTP send carried — so the echo below matches
   *  the server's queued-turn projection instead of rendering it twice. Empty
   *  alongside an empty `firstMessage`. */
  firstMessageClientId: string
  /** Whose conversation this is — named in the header, which said "Canopy". */
  agent: EmbedAgent
  /** Back to the start screen: a new question, or another earlier chat. */
  onBack: () => void
  /** canopy turned the live connection away for good — with its reason, if it
   *  gave one. The panel then says so instead of drawing a chat that cannot load. */
  onRefused: (reason: string) => void
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
  /** Whether the host lets the panel expand (its `init`). */
  expandable?: boolean
  /** Whether the host lets the docked rail snap into its page (its `init`). */
  snappable?: boolean
  /** This tab's act-as-you permission (pageGrant.ts). */
  pageGrant?: PageGrant
}) {
  /** The person acted in this conversation: make sure this tab holds a grant, and
   *  when this is what asked for one, tell canopy the page acted here (a page
   *  declaration stamps the conversation with the new token's grant) — before the
   *  turn this send starts, so the turn reads through it. A send never waits for
   *  the passkey: the tab watches, and asks only if the turn needs it. */
  const withGrant = useCallback(async () => {
    if (!pageGrant) return
    if (await pageGrant.ensure()) {
      await declarePage(client, link, sessionBase(isContact, sessionId))
    }
    pageGrant.watch()
  }, [pageGrant, client, link, isContact, sessionId])
  /** A live card needs the grant now: ask for the passkey and wait for the
   *  person's answer (confirmed or declined) before it tries again. */
  const grantForCard = useCallback(async () => {
    if (!pageGrant) return
    await withGrant()
    pageGrant.need()
    await pageGrant.settled()
  }, [pageGrant, withGrant])
  // No tool calls ever arrive here: canopy withholds them from every widget
  // connection, decided by the token rather than asked for (consumers.py).
  // A one-time ticket per connection rather than the token on the URL: URLs are
  // written to access logs, and a ticket found in one is already spent.
  const wsUrl = useCallback(
    () => client.sessionSocketTicketUrl(sessionId, { contact: isContact }),
    [client, sessionId, isContact],
  )
  // The agent asking this page to do something. Arrives on the session socket
  // as a DOORBELL only — the durable PageAction row is the mechanism, and the
  // result goes back over HTTP so it is an acknowledged write rather than a
  // second frame that could land nowhere.
  // The host's `resources/updated` for this panel's Views, relayed on the socket.
  const appUpdates = useMemo(() => createAppResourceUpdates(), [])
  // Who is asking to join this private conversation, as the socket last said.
  const [liveJoinRequests, setLiveJoinRequests] = useState<JoinRequest[] | null>(null)
  const onUnknownEvent = useCallback(
    (frame: { event: string; data?: unknown }) => {
      // A resource a View in this panel subscribed to changed at its host: the
      // View is told, and re-reads it as the visitor.
      const update = appResourceUpdate(frame)
      if (update) {
        appUpdates.emit(update.toolCallId, update.uri)
        return
      }
      const asking = joinRequestsFrame(frame)
      if (asking) {
        setLiveJoinRequests(asking)
        return
      }
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
    [client, link, sessionId, isContact, appUpdates],
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
    (typed: string, clientId: string) => {
      const context = contextPreamble.current
      contextPreamble.current = null
      const body = context ? `${typed}\n\n${context}` : typed
      return client.rest.json(`${sessionBase(isContact, sessionId)}/send`, {
        method: 'POST',
        body: JSON.stringify({ text: body, client_id: clientId }),
      })
    },
    [client, sessionId, contextPreamble, isContact],
  )
  // Unknown (null) until the session is read: memory's switches wait for it.
  const [sensitive, setSensitive] = useState<boolean | null>(null)
  const socket = useSessionSocket({
    sessionId, wsUrl, onUnknownEvent, protocol: 'ag-ui',
    sendOverHttp: isContact ? sendOverHttp : undefined,
    // A private conversation's draft stays in this frame until it is sent (and until
    // the panel knows, it is treated as one).
    localDrafts: sensitive !== false,
  })
  const menu = socket.state.menu ?? null
  const refusedReason = socket.refused?.reason
  const toldRefused = useRef(false)
  useEffect(() => {
    if (refusedReason === undefined || toldRefused.current) return
    toldRefused.current = true
    onRefused(refusedReason)
  }, [refusedReason, onRefused])
  const pageName = pageLabel(usePageState(link))
  // Agent memory for this conversation — the chat page's own toggles, through
  // the frame's client. A contact has no canopy account to own the setting, so
  // nothing is asked for them (sessionMemory.ts says who may change it).
  const memorySource = useMemo(
    () => (isContact ? null : embedMemorySource(client.rest, sessionId, currentFrameBaseUrl())),
    [client, sessionId, isContact],
  )
  // A contact on a site canopy trusts for their email may opt in themself, in this
  // frame (ContactMemory.tsx); anywhere else it renders nothing.
  const contactMemory = useMemo(() => (isContact ? contactHcp(client.rest) : null), [client, isContact])
  // Expanded, a View may use the panel's whole width and most of its height —
  // a results table or a chart is why somebody expanded it.
  const expanded = useExpanded(link)

  // A sensitive conversation arrives sealed to its people's vault keys: the panel
  // opens it here, in the frame, with the visitor's own vault key — unlocked from the
  // keyset the conversation carries (`sealed_key.keyset`), since the frame has no
  // browser session for the vault's own routes. A contact holds no key, ever.
  const [sealedKey, setSealedKey] = useState<SealedKey | null>(null)
  // Its owner answers a request to join (`can_share`); its other people see it.
  const [myRole, setMyRole] = useState<string | null>(null)
  // Who can see this conversation (`audience`, the same pieces as the session ACL).
  const [audience, setAudience] = useState<SessionAudience | null>(null)
  const sealedRows = socket.state.messages.some((m) => typeof m.sealed === 'string')
  useEffect(() => {
    if (isContact) return
    let live = true
    void client.rest.json<{
      sealed_key?: SealedKey | null; sensitive?: boolean; my_role?: string | null; origin_resource?: string
      audience?: SessionAudience | null
    }>(
      `/api/canopy-sessions/${encodeURIComponent(sessionId)}`)
      .then((d) => {
        if (!live) return
        // The page around the panel learns which conversation is open and where it
        // began — canopy's record of it, not the page's — and nothing it said.
        link.reportSession({
          id: sessionId, resource: d.origin_resource ?? '', private: Boolean(d.sensitive || d.sealed_key),
        })
        setSealedKey(d.sealed_key ?? null)
        setMyRole(d.my_role ?? null)
        setAudience(d.audience ?? null)
        setSensitive(Boolean(d.sensitive || d.sealed_key))
      })
      .catch(() => live && setSensitive((v) => v ?? false))
    return () => {
      live = false
    }
  }, [client, sessionId, isContact, sealedRows, link])
  // A contact's conversation is never private (a contact holds no key); its report
  // comes from the contact door.
  useEffect(() => {
    if (!isContact) return
    let live = true
    void client.rest.json<{ origin_resource?: string }>(sessionBase(true, sessionId))
      .then((d) => live && link.reportSession({ id: sessionId, resource: d.origin_resource ?? '', private: false }))
      .catch(() => undefined)
    return () => {
      live = false
    }
  }, [client, sessionId, isContact, link])
  const unlocker = useUnlockPrompt()
  const unlockVault = useCallback(async () => {
    if (!sealedKey?.keyset) throw new Error('Set up your vault key in canopy (Settings → Vault key) to read this.')
    return unlockLocally(sealedKey.keyset, unlocker.prompt)
  }, [sealedKey, unlocker.prompt])
  const sealed = useSealedConversation(sealedKey, socket.state.messages, unlockVault)
  const keyRef = sealed.keyRef
  // Before its key is wrapped to someone new to this browser: their fingerprint, checked.
  const personTrust = usePersonTrust()
  const keyOpen = Boolean(sealed.key)
  // Someone asking to join: the panel adds them itself — the same re-wrap canopy's
  // chat page does (unlock from the keyset, open the conversation key, wrap it to
  // them, `POST participants`), through the frame's own token.
  const joinActions = useMemo<JoinRequestActions>(() => {
    const base = `/api/canopy-sessions/${encodeURIComponent(sessionId)}`
    const people: PeopleApi = {
      getSession: (id) => client.rest.json(`/api/canopy-sessions/${encodeURIComponent(id)}`),
      participantRecipient: (id, email) => client.rest.json(
        `/api/canopy-sessions/${encodeURIComponent(id)}/participants/recipient?email=${encodeURIComponent(email)}`),
      addParticipant: (id, email, role, wrap) => client.rest.json(
        `/api/canopy-sessions/${encodeURIComponent(id)}/participants`,
        { method: 'POST', body: JSON.stringify(wrap ? { email, role, wrap } : { email, role }) }),
      giveKeyCopy: (id, userId, wrap) => client.rest.json(
        `/api/canopy-sessions/${encodeURIComponent(id)}/key-copies`,
        { method: 'POST', body: JSON.stringify({ user_id: userId, wrap }) }),
    }
    return {
      list: () => client.rest.json<JoinRequest[]>(`${base}/join-requests`),
      add: (email) => addPerson(sessionId, email, 'editor', unlockVault, people, personTrust.confirm),
      decline: (requestId) => client.rest.json(
        `/api/canopy-sessions/join-requests/${encodeURIComponent(requestId)}/decline`, { method: 'POST' }),
    }
  }, [client, sessionId, unlockVault, personTrust.confirm])
  // How this person's vault key opens here, for the banner's words; and whether this
  // frame may use a passkey at all (Chrome says, through its permissions policy).
  const unlockMethod = (sealedKey?.keyset?.seals ?? []).some((x) => (x as { alg?: string }).alg === 'prf')
    ? 'passkey' as const : 'passphrase' as const
  const framePasskeys = (document as Document & { featurePolicy?: { allowsFeature?(f: string): boolean } })
    .featurePolicy?.allowsFeature?.('publickey-credentials-get')
  const panelState = useMemo(
    () => (sealed.messages === socket.state.messages ? socket.state : { ...socket.state, messages: sealed.messages }),
    [socket.state, sealed.messages],
  )
  // Files, for a canopy user (a contact holds no key and uploads nothing). In a private
  // conversation each is sealed here, in the frame, to the conversation's people as well
  // as uploaded for the agent's one turn; until the panel knows whether it is private, a
  // file waits (refused with why) rather than go up unsealed.
  const fileApi = useMemo(() => panelAttachmentApi(client.rest), [client])
  const sealedFilesApi = useMemo(() => panelSealedFilesApi(client.rest), [client])
  // Who is uploading, bound into the seal: the keyset's own user (sent only to this
  // person's browser), else who the socket says.
  const meId = sealedKey?.keyset?.user_id ?? socket.state.current_user_id
  const privateFiles = useMemo<PrivateFiles | null>(
    () => (sealedKey && meId != null
      ? { target: { conversation: sealedKey.conversation, public_jwk: sealedKey.public_jwk as { x?: string; y?: string } },
          userId: meId }
      : null),
    [sealedKey, meId])
  const fileRefusal = sensitive === null ? 'Opening the conversation — attach the file in a moment.'
    : sensitive && !privateFiles ? PRIVATE_NO_KEY : undefined
  const { attachments, attach, remove: removeAttachment, clear: clearAttachments } =
    useAttachments(sessionId, fileRefusal, sensitive ? privateFiles : null, fileApi)
  const userLines = socket.state.messages.filter((m) => m.role === 'user').length
  const sealedFiles = useSealedAttachments(sessionId, Boolean(sensitive) && !isContact, userLines, sealedFilesApi)
  const attachmentHost = useMemo(
    () => sealedAttachmentHost(sealedFiles, sealed.key, () => void sealed.unlock(), sealedFilesApi),
    [sealedFiles, sealed.key, sealed.unlock, sealedFilesApi])
  const appHost = useMemo(
    () => sealedAppHost(embedAppHost(client, sessionBase(isContact, sessionId), expanded, appUpdates,
                                     pageGrant ? { forCard: grantForCard, forSend: withGrant } : undefined),
                        () => keyRef.current, keyOpen),
    [client, sessionId, isContact, expanded, keyRef, appUpdates, keyOpen, pageGrant, withGrant, grantForCard])

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
    socket.noteLocalSend(firstMessage, firstMessageClientId)
  }, [firstMessage, firstMessageClientId, sessionId, socket])

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

  const onSend = useCallback((body: string) => {
    // The page snapshot rides the FIRST message rather than an opening turn of
    // its own. An automatic turn on open would claim a runner and produce an
    // agent reply before the user had said anything — and metadata alone never
    // reaches the agent, since canopy treats it as an opaque bag.
    // The ask leads here too. This path only runs for a message sent after the
    // session already exists, so it does not name an emdash task — but a reader
    // scrolling the transcript should still see the question before the JSON.
    const context = contextPreamble.current
    contextPreamble.current = null
    // The server takes whatever files this person staged with the send.
    clearAttachments()
    void withGrant().catch(() => undefined).then(() => socket.sendChat(context ? `${body}\n\n${context}` : body))
  }, [socket, contextPreamble, clearAttachments, withGrant])
  const onContactSend = useCallback((body: string) => {
    void withGrant().catch(() => undefined).then(() => socket.sendChat(body))
  }, [socket, withGrant])

  return (
    <div className="flex h-full flex-col">
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
      <ConversationFacts pageName={pageName} audience={audience} sensitive={Boolean(sensitive)}
        pageGrant={pageGrant} agentName={agent.name} />
      <SealedConversationBanner
        state={sealed.state}
        error={sealed.error}
        warning={sealed.warning}
        onDismissWarning={sealed.dismissWarning}
        onUnlock={() => void sealed.unlock()}
        unlockElement={unlocker.element}
        hasSealed={sealed.hasSealed}
        method={unlockMethod}
        // Only when unlocking cannot work in this frame — the browser will not let it
        // use a passkey, or the unlock failed — is it read in canopy itself, where the
        // vault has its own page.
        openElsewhere={((framePasskeys === false || !passkeysWorkHere()) && unlockMethod === 'passkey') || sealed.state === 'error' ? (
          <a href={`${currentFrameBaseUrl()}/w/${encodeURIComponent(agent.workspace)}/sessions/${encodeURIComponent(sessionId)}`}
             target="_blank" rel="noopener noreferrer" className="text-primary underline">
            Read it in canopy
          </a>
        ) : undefined}
      />
      {sensitive && !isContact && (
        <JoinRequests sessionId={sessionId} actions={joinActions} live={liveJoinRequests}
          canDecide={myRole === 'owner'} unlockElement={personTrust.element} />
      )}
      <div className="min-h-0 flex-1">
        <ChatPanel
          // MCP Apps: a Connected site's View for a tool result, acting as this
          // visitor (their own host grant), narrow-panel sized.
          appHost={appHost}
          // A private conversation's files, sealed: opened here with the conversation key.
          attachmentHost={attachmentHost}
          {...(isContact ? {} : { attachments, onAttach: attach, onRemoveAttachment: removeAttachment })}
          state={panelState}
          connected={socket.connected}
          currentUserId={socket.state.current_user_id}
          onSend={isContact ? onContactSend : onSend}
          onStop={isContact ? () => undefined : socket.stopChat}
          onUnqueue={isContact ? undefined : socket.unqueueChat}
          // `socket.awaitingReply` on BOTH paths now. It used to be `sending`
          // for a contact, which tracked the HTTP request and so cleared the
          // instant the POST returned — an indicator for ~200ms, then silence
          // for the actual wait. `noteLocalSend` raises it and only a stream
          // frame (or the server's settled status) lowers it.
          awaitingReply={socket.awaitingReply}
          onUpdateDraft={socket.updateDraft}
          onDiscard={isContact ? () => socket.updateDraft('') : socket.discardDraft}
          // The widget has no ⋯ menu (canopy-web#1004) — the composer control
          // it used to sit on is gone from ChatPage too, so this stays
          // omitted rather than pointing at a control nobody can reach. The
          // session keeps whatever mode is already stored
          // (readStoredTypingVisibility) or defaults to "live"; nothing here
          // needs to change it.
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


/** The embed panel's MCP Apps host: the same five routes as canopy's chat page,
 *  under `/api/contact/` for a contact (their whole surface) — through the
 *  panel's own client, so the visitor's token is what authorises every click. */
function embedAppHost(client: CanopyClient, sessionPath: string, expanded = false,
                      updates?: AppResourceUpdates,
                      grant?: { forCard: () => Promise<void>, forSend: () => Promise<void> }): AppHost {
  const withGrant = grant?.forCard
  const at = (callId: string) => `${sessionPath}/apps/${encodeURIComponent(callId)}`
  // A View reading the site as the person needs this tab's act-as-you permission.
  // Opening their own conversation is the person's act, so a card that finds none
  // asks for one (the panel then shows "can act as you … · Stop") and tries once more.
  const granted = async <T,>(go: () => Promise<T>): Promise<T> => {
    try {
      return await go()
    } catch (error) {
      if (!withGrant || !isNoGrant(error)) throw error
      await withGrant()
      return go()
    }
  }
  const send = <T,>(path: string, method: string, body: unknown) =>
    granted(() => client.rest.json(path, { method, body: JSON.stringify(body) }) as Promise<T>)
  return {
    // A card that loads read-only for want of a grant asks for one, then loads again.
    load: async (callId) => {
      const read = () => client.rest.json(`${at(callId)}/resource`) as Promise<AppViewResource>
      const res = await read()
      if (res.can_act || !withGrant) return res
      await withGrant()
      return read()
    },
    callTool: (callId, name, args) =>
      send<CallToolResult>(`${at(callId)}/call`, 'POST', { name, arguments: args }),
    readResource: (callId, uri) => send(`${at(callId)}/read`, 'POST', { uri }),
    updateContext: (callId, params) =>
      send<void>(`${at(callId)}/context`, 'PUT', params).then(() => undefined),
    // A View's ui/message is the person sending: the grant first, as for a typed send.
    sendMessage: (callId, text) =>
      (grant ? grant.forSend() : Promise.resolve())
        .then(() => send<void>(`${at(callId)}/message`, 'POST', { text, client_id: newClientId() }))
        .then(() => undefined),
    subscribe: (callId, uri) => send<void>(`${at(callId)}/subscribe`, 'POST', { uri }).then(() => undefined),
    unsubscribe: (callId, uri) => send<void>(`${at(callId)}/unsubscribe`, 'POST', { uri }).then(() => undefined),
    ...(updates ? { onResourceUpdated: updates.onResourceUpdated } : {}),
    resolveUrl: (path) => (/^https?:\/\//.test(path) ? path : `${currentFrameBaseUrl()}${path}`),
    // Narrow-panel sized, until the visitor expands the panel: then the View
    // gets the panel's width (no cap) and most of the window's height.
    ...(expanded
      ? { maxHeight: Math.max(900, Math.round(window.innerHeight * 0.85)) }
      : { maxWidth: 420 }),
    // A View may ask to go full screen: it is drawn over the panel.
    fullscreen: true,
  }
}


/** A confirmed act-as-you permission while it is live, else null. */
function useConfirmedGrant(pageGrant: PageGrant | undefined) {
  const [grant, setGrant] = useState(() => pageGrant?.current() ?? null)
  useEffect(() => {
    if (!pageGrant) return
    setGrant(pageGrant.current())
    return pageGrant.subscribe(() => setGrant(pageGrant.current()))
  }, [pageGrant])
  return grant?.confirmed ? grant : null
}


/** The conversation's facts in one line under the header — private or not, the
 *  page, who can read it, the act-as-you permission — with the full sentences a
 *  click away. Stop stays on the line: ending a permission is never behind a click. */
function ConversationFacts({ pageName, audience, sensitive, pageGrant, agentName }: {
  pageName: string
  audience: SessionAudience | null
  sensitive: boolean
  pageGrant?: PageGrant
  agentName: string
}) {
  const [open, setOpen] = useState(false)
  const [stopping, setStopping] = useState(false)
  const grant = useConfirmedGrant(pageGrant)
  const summary = [sensitive ? 'Private' : '', pageName, audience?.label ?? ''].filter(Boolean)
  if (!summary.length && !grant) return null
  const who = grant ? grant.agent_name || agentName : ''
  const until = grant ? untilLabel(grant.expires_at) : ''
  return (
    <div className="border-b border-border px-3 py-1 text-[11px] text-muted-foreground" data-testid="conversation-facts">
      <div className="flex items-center gap-2">
        <button type="button" onClick={() => setOpen((o) => !o)} aria-expanded={open}
                title={open ? 'Hide details' : 'Show details'}
                className="flex min-w-0 flex-1 items-center gap-1 text-left hover:text-foreground">
          {sensitive && <Lock className="h-3 w-3 shrink-0" aria-hidden />}
          <span className="min-w-0 truncate">{summary.join(' · ')}</span>
          <ChevronDown className={`h-3 w-3 shrink-0 transition-transform ${open ? 'rotate-180' : ''}`} aria-hidden />
        </button>
        {grant && pageGrant && (
          <span className="flex shrink-0 items-center gap-1" data-testid="page-grant-line"
                title={`What ${grant.site} lets ${who} read as you: ${grant.scope || 'this page'}`}>
            <span>{who} can act as you{until ? ` until ${until}` : ''}</span>
            <span aria-hidden>·</span>
            <button type="button" disabled={stopping}
                    onClick={() => { setStopping(true); void pageGrant.stop().finally(() => setStopping(false)) }}
                    className="text-destructive hover:underline disabled:opacity-50">
              Stop
            </button>
          </span>
        )}
      </div>
      {open && (
        <div className="mt-1 space-y-0.5" data-testid="conversation-facts-details">
          {pageName && (
            <p className="m-0" data-testid="asking-about">
              Asking about <span className="text-foreground">{pageName}</span>
            </p>
          )}
          <SessionAudienceLine audience={audience} />
          {sensitive && <p className="m-0" data-testid="sensitive-details">{PRIVATE_LINE}. {PRIVATE_DETAILS}</p>}
          {grant && (
            <p className="m-0">
              {who} can act as you on this page ({grant.scope || 'this page'} on {grant.site})
              {until ? ` until ${until}` : ''}.
            </p>
          )}
        </div>
      )}
    </div>
  )
}


/** The passkey prompt for an act-as-you permission the page is waiting on, shown only
 *  once something needs it: the agent's turn tried to act as the person, or a live
 *  card did (`pageGrant.asking`). Nothing the host granted is usable until the
 *  person confirms here, in canopy's frame; "Not now" stops it, and the agent is
 *  told so when it tries again. Its own button, because
 *  Safari runs WebAuthn only inside a click. Where the frame cannot use a passkey
 *  (the Canopy Runner app), or the passkey failed here, the person confirms the same
 *  grant on canopy's page in their browser, and this prompt closes when it sees that
 *  (`passkeyHere.ts`). */
function PageGrantConfirm({ pageGrant, agentName }: { pageGrant: PageGrant; agentName: string }) {
  const [grant, setGrant] = useState(() => pageGrant.current())
  const [asking, setAsking] = useState(() => pageGrant.asking())
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [inBrowser, setInBrowser] = useState(false)
  useEffect(() => pageGrant.subscribe(() => {
    setGrant(pageGrant.current())
    setAsking(pageGrant.asking())
  }), [pageGrant])
  useWhileAway(() => pageGrant.refresh(), inBrowser && asking)
  // Only once something needs it (the turn or a card): a send alone never asks.
  if (!grant || !asking) return null
  const who = grant.agent_name || agentName
  const here = passkeysWorkHere()
  const confirmInBrowser = () => {
    openInBrowser(`/confirm/page-permission/${grant.id}`)
    setError(null)
    setInBrowser(true)
  }
  const confirm = () => {
    setBusy(true)
    setError(null)
    pageGrant.confirm()
      // canopy's own sentence (no passkey yet: where to register one), else the browser's.
      .catch((e: unknown) => setError((e as { detail?: string })?.detail
        || (e instanceof Error && e.name !== 'NotAllowedError' ? e.message : '')
        || 'The passkey was not confirmed.'))
      .finally(() => setBusy(false))
  }
  const decline = () => {
    setBusy(true)
    void pageGrant.stop().finally(() => setBusy(false))
  }
  return (
    <div role="dialog" aria-modal="true" aria-label="Confirm act as you"
         className="fixed inset-0 z-50 flex items-center justify-center bg-background/80 p-4"
         data-testid="page-grant-confirm">
      <div className="w-full max-w-sm space-y-3 rounded-lg border border-border bg-card p-4">
        <p className="text-sm text-foreground">
          Let {who} act as you on this page for 30 minutes?
        </p>
        <p className="text-[12px] text-muted-foreground">
          It can do what you can do on {grant.site} for this page ({grant.scope || 'this page'}),
          and nothing more. Confirm with your passkey; you can stop it at any time.
        </p>
        {inBrowser ? (
          <p className="text-[12px] text-foreground" data-testid="page-grant-in-browser">
            Waiting for you to confirm in your browser…
          </p>
        ) : !here && (
          <p className="text-[12px] text-muted-foreground">{PASSKEY_IN_BROWSER}</p>
        )}
        {error && (
          <p className="text-[12px] text-destructive">
            {error}{' '}
            <button type="button" onClick={confirmInBrowser} className="underline"
                    data-testid="page-grant-try-browser">
              Confirm in your browser instead
            </button>
          </p>
        )}
        <div className="flex justify-end gap-2">
          <button type="button" onClick={decline} disabled={busy}
                  className="rounded px-2 py-1 text-[12px] text-muted-foreground hover:bg-muted disabled:opacity-50">
            Not now
          </button>
          <button type="button" onClick={here && !inBrowser ? confirm : confirmInBrowser} disabled={busy}
                  className="rounded bg-primary px-3 py-1 text-[12px] text-primary-foreground hover:opacity-90 disabled:opacity-50">
            {inBrowser ? 'Open it again' : here ? 'Confirm with passkey' : 'Confirm in your browser'}
          </button>
        </div>
      </div>
    </div>
  )
}
