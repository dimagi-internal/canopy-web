import { ChatPanel, MenuPrompt, useSessionSocket } from 'canopy-ui/chat'
import { type CanopyClient } from 'canopy-client'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { SealedConversationBanner, sealedAppHost } from '@/components/chat/SealedConversationBanner'
import { unlockLocally } from '@/vault/localUnlock'
import { usePersonTrust } from '@/vault/PersonTrustPrompt'
import { useUnlockPrompt } from '@/vault/UnlockPrompt'
import { useSealedConversation } from '@/vault/useSealedConversation'
import { menuBlocksComposer } from '@/pages/chatPageLogic'
import { currentFrameBaseUrl } from './frameBase'
import type { HostLink } from './hostLink'
import { contactHcp, embedMemorySource } from './sessionMemory'
import { pageLabel } from './conversationLabels'
import { JoinRequests } from '@/components/chat/JoinRequests'
import { PRIVATE_NO_KEY, useAttachments, type PrivateFiles } from '@/pages/chat/useAttachments'
import { sealedAttachmentHost, useSealedAttachments } from '@/components/chat/SealedAttachments'
import { panelAttachmentApi, panelSealedFilesApi } from './panelFiles'
import { type PageGrant } from './pageGrant'
import { passkeysWorkHere } from '@/auth/passkeyHere'
import { type EmbedAgent } from './types'
import { sessionBase } from './session'
import { renderMarkdown, usePageState } from './panelBits'
import { useExpanded } from './HeaderButtons'
import { embedAppHost } from './embedAppHost'
import { ConversationFacts } from './ConversationFacts'
import { EmbedChatHeader } from './EmbedChatHeader'
import {
  useJoinActions,
  useMenuAnswer,
  usePageDeclarations,
  usePageGrantActions,
  usePanelFrames,
  useSessionDetail,
} from './embedChatHooks'

export function EmbedChat({
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
  const { withGrant, grantForCard } = usePageGrantActions({ pageGrant, client, link, isContact, sessionId })
  // No tool calls ever arrive here: canopy withholds them from every widget
  // connection, decided by the token rather than asked for (consumers.py).
  // A one-time ticket per connection rather than the token on the URL: URLs are
  // written to access logs, and a ticket found in one is already spent.
  const wsUrl = useCallback(
    () => client.sessionSocketTicketUrl(sessionId, { contact: isContact }),
    [client, sessionId, isContact],
  )
  const { appUpdates, liveJoinRequests, onUnknownEvent } = usePanelFrames({ client, link, sessionId, isContact, pageGrant })

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

  const sealedRows = socket.state.messages.some((m) => typeof m.sealed === 'string')
  const { sealedKey, myRole, audience } = useSessionDetail({ client, sessionId, isContact, link, sealedRows, setSensitive })
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
  const joinActions = useJoinActions({ client, sessionId, unlockVault, confirmPerson: personTrust.confirm })
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

  usePageDeclarations(client, link, sessionId)

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

  const { answering, answerError, onAnswerMenu } = useMenuAnswer(client, sessionId)

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
      <EmbedChatHeader
        agent={agent}
        onBack={onBack}
        sessionId={sessionId}
        sensitive={sensitive}
        memorySource={memorySource}
        contactMemory={contactMemory}
        link={link}
        snappable={snappable}
        expandable={expandable}
      />
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
