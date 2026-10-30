import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { SessionAudienceLine } from '@/components/SessionAudienceLine'
import { clsx } from 'clsx'
import { useParams } from 'react-router-dom'
import {
  appResourceUpdate,
  ChatPanel,
  createAppResourceUpdates,
  PlacementBanner,
  MenuPrompt,
  turnPhase,
  useSessionSocket,
  type SecretRequestHost,
} from 'canopy-ui/chat'
import { renderChatMarkdown } from '@/components/chat/renderChatMarkdown'
import { SecretSealCard } from '@/components/vault/SecretSealCard'
import { wsUrl } from '@/lib/wsUrl'
import {
  getSession,
  attachSession,
  detachSession,
  sendMessage,
  appHostFor,
  declineJoinRequest,
  listJoinRequests,
  setAsideMyKeyCopy,
  type ChatSession,
  type JoinRequest,
} from '@/api/chat'
import { ChatSessionMenu } from '@/components/chat/ChatSessionMenu'
import { MessageContextMenu } from '@/components/chat/MessageContextMenu'
import { useSessionMemory } from '@/components/chat/SessionMemoryToggles'
import { TitleInput } from '@/components/chat/TitleInput'
import { sessionTargetLabel } from '@/components/chat/sessionTargetLabel'
import { menuBlocksComposer, restToKitMessage, sendBlockReason, shouldShowLoadFull } from './chatPageLogic'
import { PRIVATE_NO_KEY, useAttachments, type PrivateFiles } from './chat/useAttachments'
import { sealedAttachmentHost, useSealedAttachments } from '@/components/chat/SealedAttachments'
import { SealedConversationBanner, sealedAppHost } from '@/components/chat/SealedConversationBanner'
import { PrivateConversationNote } from '@/components/chat/PrivateConversationNote'
import { PrivateConversationGate } from '@/components/chat/PrivateConversationGate'
import { JoinRequests, joinRequestsFrame, type JoinRequestActions } from '@/components/chat/JoinRequests'
import { addPerson } from '@/components/chat/ChatPeople'
import { usePersonTrust } from '@/vault/PersonTrustPrompt'
import { privateTitle } from '@/components/chat/privateConversation'
import { useVaultUnlock } from '@/vault/useVaultUnlock'
import { useSealedConversation } from '@/vault/useSealedConversation'
import type { ConversationKey, SealedKey } from '@/vault/sealedChat'
import { useMenuAnswer } from './chat/useMenuAnswer'
import { useMessageFork } from './chat/useMessageFork'
import { useRunnerPlacement } from './chat/useRunnerPlacement'
import { useSessionActions } from './chat/useSessionActions'
import { useSessionMeta } from './chat/useSessionMeta'
import { useSlashCommands } from './chat/useSlashCommands'
import { CanopyAIButton } from '@/widget/CanopyAIButton'
import { useTranscriptPaging } from './chat/useTranscriptPaging'
import { openInBrowser, passkeysWorkHere } from '@/auth/passkeyHere'
import { cachedMessages, rememberMessages } from './chat/sessionCache'

/**
 * Standalone live-chat route (/w/:workspace/sessions/:id). Wires canopy's
 * WebSocket URL + REST session-meta/history/liveness + react-markdown into the
 * reusable `canopy-ui/chat` ChatPanel. All live chat state lives in
 * `useSessionSocket`; this page supplies seams (attach/detach, scroll-back,
 * full backfill, running/idle) + a minimal title shell.
 *
 * Also the right pane of /ide/:id, where `onLeave` replaces the page's own
 * navigation: a closed session drops back to the tree instead of out of /ide,
 * and a move between workspaces keeps the /ide URL (it names no workspace).
 * `compact` is /ide's denser header: title, target and status on one line.
 * `sessionPath` is where another session opens (a fork made here); /ide keeps
 * it in /ide.
 */
// Why Share to Slack is off in a sensitive conversation (the server refuses it too).
const SENSITIVE_NO_SHARE = 'A sensitive conversation is never shared to Slack.'

/** The chat, behind the private-conversation interstitial: a sensitive conversation
 *  is opened on purpose, and someone not in it may only ask to be added. */
export function ChatPage(props: Parameters<typeof ChatPageInner>[0] = {}) {
  const { id = '' } = useParams()
  return (
    <PrivateConversationGate key={id} sessionId={id}>
      <ChatPageInner {...props} />
    </PrivateConversationGate>
  )
}

function ChatPageInner({
  onLeave,
  compact = false,
  sessionPath,
  editTitle = false,
  onTitleChange,
}: {
  onLeave?: () => void
  compact?: boolean
  sessionPath?: (s: ChatSession) => string
  /** Open with the title already being renamed — /ide's just-created session. */
  editTitle?: boolean
  /** A rename landed, from here or anywhere else (the socket says so). */
  onTitleChange?: () => void
} = {}) {
  const { id = '' } = useParams()
  // A sensitive conversation's Views get their call input decrypted here, with the
  // conversation key once it is open (`sealed`, below).
  const sealedKeyRef = useRef<ConversationKey | null>(null)
  // A host's `resources/updated`, relayed on the socket, fanned out to the Views.
  const appUpdates = useMemo(() => createAppResourceUpdates(), [])
  // Whether the conversation key is open in this tab: a View loads again when it opens.
  const [keyOpen, setKeyOpen] = useState(false)
  const appHost = useMemo(
    () => sealedAppHost(appHostFor(id, appUpdates), () => sealedKeyRef.current, keyOpen), [id, appUpdates, keyOpen])
  // The agent asked for one of its secrets: a card that seals it in this
  // browser. Once stored, a value-free note tells the agent to carry on.
  const secretRequestHost = useMemo<SecretRequestHost>(() => ({
    render: (ref) => (
      <SecretSealCard key={ref.id} slug={ref.agent} name={ref.name} requestId={ref.id}
        onStored={(n) => void sendMessage(id, `${n} is stored for ${ref.agent} — sealed in my browser; `
          + 'resolve it the usual way.').catch(() => undefined)} />
    ),
  }), [id])
  const onTitleChangeRef = useRef(onTitleChange)
  onTitleChangeRef.current = onTitleChange
  // Your memory in this session lives in the ⋯ menu; null hides it.
  const memory = useSessionMemory(id)
  // Session meta seeds the scroll-back cursor (the WS `session.state` snapshot
  // doesn't carry it — it's frozen to the tail page it was built from). Called
  // once the fetch lands, after `paging` below exists.
  const { meta, setMeta, metaError, agentName } = useSessionMeta(id, (m) => paging.seed(m))
  // Who is asking to join this private conversation, as the socket last said.
  const [liveJoinRequests, setLiveJoinRequests] = useState<JoinRequest[] | null>(null)

  // AG-UI on the wire, canopy frames in memory: `fromAgui` turns the protocol
  // back into the same `WsEvent`s the reducer has always consumed, and
  // `agui.test.ts` proves the reducer reaches identical state either way.
  const socket = useSessionSocket({
    sessionId: id,
    wsUrl,
    protocol: 'ag-ui',
    // A private conversation's draft stays in this browser until it is sent.
    localDrafts: Boolean(meta?.sensitive),
    // `app.resource_updated`: a resource a View subscribed to changed at its host.
    // `session.join_requests`: someone asked to join (or was answered), live.
    onUnknownEvent: useCallback((frame: { event: string; data?: unknown }) => {
      const update = appResourceUpdate(frame)
      if (update) appUpdates.emit(update.toolCallId, update.uri)
      const asking = joinRequestsFrame(frame)
      if (asking) setLiveJoinRequests(asking)
    }, [appUpdates]),
    // A socket send with no receipt is resent here, under the same client_id,
    // so it cannot become a second turn (see canopy-ui's sendChat).
    resendOverHttp: useCallback(
      (text: string, clientId: string) => sendMessage(id, text, clientId),
      [id],
    ),
    // A rename made elsewhere (the /ide list, another tab, autotitle): `meta` is
    // fetched once, so re-read just the title.
    onTitleUpdated: useCallback(() => {
      getSession(id)
        .then((m) => setMeta((prev) => (prev ? { ...prev, title: m.title } : prev)))
        .catch(() => {})
      onTitleChangeRef.current?.()
    }, [id, setMeta]),
  })

  // A sensitive conversation arrives sealed: the words are opened here, with the
  // person's vault key, and nowhere else (vault/sealedChat.ts).
  const vault = useVaultUnlock()
  // Before its key is wrapped to someone new to this browser: their fingerprint, checked.
  const personTrust = usePersonTrust()
  const sealed = useSealedConversation(
    (meta?.sealed_key ?? null) as SealedKey | null, socket.state.messages, vault.unlock)
  // Adding someone who asked is this browser's act: it wraps them the conversation key.
  const joinActions = useMemo<JoinRequestActions>(() => ({
    list: () => listJoinRequests(id),
    add: (email) => addPerson(id, email, 'editor', vault.unlock, undefined, personTrust.confirm),
    decline: declineJoinRequest,
  }), [id, vault.unlock, personTrust.confirm])
  sealedKeyRef.current = sealed.key
  // A private conversation's file is sealed here to its people (vault/sealedFiles.ts) as
  // well as uploaded for the agent's one turn; with no key to seal to, it is refused.
  const sealTarget = meta?.sensitive ? meta.sealed_key ?? null : null
  const meId = socket.state.current_user_id
  const privateFiles = useMemo<PrivateFiles | null>(
    () => (sealTarget && meId != null
      ? { target: { conversation: sealTarget.conversation, public_jwk: sealTarget.public_jwk as { x?: string; y?: string } },
          userId: meId }
      : null),
    [sealTarget, meId])
  const { attachments, attach, remove: removeAttachment, clear: clearAttachments } =
    useAttachments(id, meta?.sensitive && !privateFiles ? PRIVATE_NO_KEY : undefined, privateFiles)
  // Its sent files, under the lines they went with: "Attachment" until the key opens.
  const userLines = socket.state.messages.filter((m) => m.role === 'user').length
  const sealedFiles = useSealedAttachments(id, Boolean(meta?.sensitive), userLines)
  const attachmentHost = useMemo(
    () => sealedAttachmentHost(sealedFiles, sealed.key, () => void sealed.unlock()),
    [sealedFiles, sealed.key, sealed.unlock])
  const keyIsOpen = Boolean(sealed.key)
  useEffect(() => setKeyOpen(keyIsOpen), [keyIsOpen])
  const panelState = useMemo(
    () => (sealed.messages === socket.state.messages ? socket.state : { ...socket.state, messages: sealed.messages }),
    [socket.state, sealed.messages],
  )

  const paging = useTranscriptPaging(id, socket.prependMessages)

  // Paint before the socket's snapshot lands: from this tab's last copy of the chat
  // (`sessionCache`) at once, else from the REST detail's tail as soon as it is read.
  // The snapshot then replaces either (`session.state`), so nothing here is ever the
  // record; it only fills the wait. Never for a private conversation, which reads
  // back only through its sealed path and is never cached.
  const { prependMessages } = socket
  useEffect(() => {
    const cached = cachedMessages(id)
    if (cached) prependMessages(cached)
  }, [id, prependMessages])
  // Not `!socket.connected`: the socket opens well before its snapshot (that is
  // the slow part, ~0.6s on labs against ~0.2s for the REST read), and an empty
  // transcript is what "no snapshot yet" looks like — REST and the snapshot hold the
  // same tail (tests/test_transcript_parity.py), so an empty one stays empty.
  const restTail = meta && !meta.sensitive ? meta.messages : null
  const noTranscriptYet = socket.state.messages.length === 0
  useEffect(() => {
    if (restTail?.length && noTranscriptYet) prependMessages(restTail.map(restToKitMessage))
  }, [restTail, noTranscriptYet, prependMessages])
  // …and keep that copy current for next time, once the socket has spoken for it.
  const live = socket.connected && meta !== null && !meta.sensitive
  useEffect(() => {
    if (live && socket.state.messages.length) rememberMessages(id, socket.state.messages)
  }, [id, live, socket.state.messages])
  const placement = useRunnerPlacement(id, meta, setMeta)
  const { boundOffline, boundRunner, boundPaused } = placement
  const fork = useMessageFork(id, meta?.runner_name, placement.fleetRunners, panelState.messages, sessionPath)
  const menuAnswer = useMenuAnswer(id, socket.state.menu, socket.state.activity)
  const { pendingAnswer, menuHidden, visibleMenu } = menuAnswer
  const actions = useSessionActions({ sessionId: id, meta, setMeta, onLeave, onTitleChange, editTitle })
  const slashCommands = useSlashCommands(
    id, meta, `${meta?.runner_name ?? ''}|${socket.state.messages.length > 0}`)

  // Attach-on-open / detach-on-unmount. The server's attach counter floors at
  // 0 rather than going negative, so a detach that lands BEFORE its paired
  // attach is silently absorbed — the attach then still increments, leaving
  // the count stuck net +1 and a runner-bound session's `stream_desired`
  // never clears. These are fire-and-forget HTTP calls with no other
  // ordering guarantee, so we chain the detach off the attach promise
  // (`.finally`, not `.then`/`.catch`, so a failed attach still detaches):
  // the detach request is never even issued until the attach one has
  // settled, which makes the wrong-order race structurally impossible.
  // React StrictMode's mount/unmount/remount double-invoke is fine here —
  // each attach/detach pair is still strictly ordered within itself.
  useEffect(() => {
    if (!id) return
    const attached = attachSession(id).catch(() => {
      /* non-fatal: a failed attach just means no bound runner to notify */
    })
    return () => {
      void attached.finally(() => {
        void detachSession(id).catch(() => {
          /* non-fatal */
        })
      })
    }
  }, [id])

  // The composer refuses rather than queueing — see sendBlockReason.
  // A viewer was given this chat to read (canopy_sessions.access); the server
  // refuses their send with a 403, so say so here instead of letting it bounce.
  const disabledReason =
    meta?.my_role === 'viewer'
      ? "You were given this chat as a viewer, so you can read it but not send."
      : sendBlockReason({
          runnerName: meta?.runner_name,
          boundOffline,
          paused: boundPaused,
          blockedOnMenu: menuBlocksComposer(visibleMenu),
        })
  // "Others see" moved from the composer into the ⋯ menu (canopy-web#1004) —
  // an owner/editor has a composer for it to describe; a viewer does not.
  const canEditTypingVisibility = actions.canManage
  const canDetachSlack = actions.canManage
  const canRename = actions.canManage

  // Until the socket is up the transcript is not empty, it is unknown: saying
  // "Start the conversation" over a session still loading read as a blank chat.
  const loadingTranscript = !socket.connected && socket.state.messages.length === 0
  const emptyState = useMemo(
    () =>
      loadingTranscript ? (
        <div
          className="flex h-full flex-col items-center justify-center gap-3 p-8 text-center text-sm text-muted-foreground"
          aria-busy="true"
          data-testid="chat-loading"
        >
          <span className="h-6 w-6 animate-spin rounded-full border-2 border-muted border-t-primary" aria-hidden="true" />
          <div>Loading the conversation…</div>
        </div>
      ) : (
        <div className="flex h-full flex-col items-center justify-center gap-1 p-8 text-center text-sm text-muted-foreground">
          <div className="text-foreground">Start the conversation</div>
          <div className="text-xs">Type a message below to begin.</div>
        </div>
      ),
    [loadingTranscript],
  )

  // "Load all messages"/"Load full session" moved into the ⋯ menu (ChatSessionMenu,
  // below) — Jonathan, 2026-10-05: they used to live here as a strip PINNED
  // above the transcript whenever the server held more history, which on an
  // old session is most of the time, for a control almost never tapped. Only
  // the WARNING stays pinned: it's a fact about what's on screen, not an
  // action, so it still renders nothing when there's nothing to say.
  const showLoadFull = shouldShowLoadFull({
    runnerName: meta?.runner_name,
    hasMoreBefore: paging.hasMoreBefore,
    historyUnavailable: paging.historyUnavailable,
  })

  const historySlot = !paging.historyUnavailable ? undefined : (
    <div className="flex flex-col items-center gap-1 border-b border-border py-2">
      <p className="rounded-md border border-warning/30 bg-warning/10 px-3 py-1.5 text-[12px] text-warning">
        Full history unavailable — runner offline. Showing the latest messages.
      </p>
    </div>
  )

  // The header says what the transcript's own bubble says: both read the kit's
  // `turnPhase` (server turn status, then the hook, then `awaitingReply` for the
  // instant after a send). The header used to derive its own from
  // `awaitingReply` alone, so it could say "queued" over a turn the server had
  // settled, with nothing on screen to correct it (2026-10-30).
  const phase = turnPhase({
    status: socket.state.turn_status,
    activity: socket.state.activity,
    awaitingReply: socket.awaitingReply,
    replying: socket.state.messages.some((m) => m.role !== 'user' && m.status === 'streaming'),
  });
  // The agent asked for a human and stopped — a permission prompt, or an idle
  // wait for input. It previously rendered as "running", which is the worst way
  // to get this wrong: you wait on an agent that is waiting on you.
  //
  // Except once you have answered: `Notification` fires on the way IN to a wait
  // and nothing fires on the way out, so `activity` stays "blocked" until the
  // agent's next row lands. Your answer is the event that ends the wait, and it
  // happened here, so it is shown here, as running.
  const answeredResuming = pendingAnswer !== null && !visibleMenu;
  const liveBlocked = phase === 'blocked' && !answeredResuming;
  const liveWorking = phase === 'working';
  const liveQueued = phase === 'queued';
  // `meta` is fetched ONCE per session id and never refreshed, and `running`
  // derives from a 120s window on the runner's last report — so this is a
  // PAGE-LOAD SNAPSHOT that can only decay. It used to outrank everything below
  // `blocked`, which quietly cost #490 its whole point: open a chat you were
  // just using and the header already said "running", so pressing send changed
  // nothing on screen and the "queued" branch was unreachable. Demoted to the
  // last resort — consulted only while nothing live has spoken (no hook, no
  // turn status), and dropped for good once anything has.
  const staleRunning =
    socket.state.activity === undefined && socket.state.turn_status == null && Boolean(meta?.running);
  const title = meta?.sensitive ? privateTitle(meta) : meta?.title?.trim() || 'Chat'

  // "with <agent>" / the project name / "no agent" — the same tag the Feed and
  // the Sessions list show per row, missing from the one place you most want
  // it: a session you're actually inside, with only its (often generic) title
  // to say what it's for (Jonathan, 2026-10-05). Waits for `meta` so it never
  // flashes "no agent" while the session is still loading.
  const targetLabel = meta ? sessionTargetLabel(agentName, meta.project) : null

  return (
    <div className="flex h-full flex-col">
      {/* The header is for READING: the title and whether the agent is working.
          Actions live in ChatSessionMenu. Five buttons here once ran 145px past
          a phone screen and scrolled the whole page sideways. */}
      <div
        className={clsx(
          'flex items-center gap-2 border-b border-border bg-background',
          compact ? 'px-3 py-1' : 'px-4 py-2',
        )}
      >
        <div className={clsx('min-w-0', compact && 'flex items-baseline gap-1.5')}>
          {actions.renaming && meta && canRename ? (
            <TitleInput
              initial={meta.title ?? ''}
              onSave={actions.renameTo}
              onCancel={() => actions.setRenaming(false)}
              className={clsx('w-64 font-semibold', compact ? 'text-[13px]' : 'text-sm')}
            />
          ) : canRename ? (
            <h1 className={clsx('truncate font-semibold text-foreground', compact ? 'text-[13px]' : 'text-sm')}>
              <button
                type="button"
                onClick={() => actions.setRenaming(true)}
                title="Rename this session"
                data-testid="session-title"
                className="max-w-full truncate rounded px-0.5 text-left hover:bg-muted"
              >
                {title}
              </button>
            </h1>
          ) : (
            <h1 className={clsx('truncate font-semibold text-foreground', compact ? 'text-[13px]' : 'text-sm')}>
              {title}
            </h1>
          )}
          {targetLabel && (
            <p className="truncate text-[11px] text-muted-foreground">
              {compact && '· '}
              {targetLabel}
            </p>
          )}
        </div>
        {/* Live agent activity beats the server's liveness fields when a hook has
            reported. `meta.running` derives from the runner's session report —
            emdash's own last_interacted_at, on a report cycle with a 120s window
            — so it could not show "working" during the part of a turn where
            Claude is thinking and nothing has been output yet. The hook fires the
            instant the turn starts. Falls back to `meta.running` when no hook has
            spoken (an older runner, or forwarding off). */}
        {liveBlocked ? (
          <span className="flex shrink-0 items-center gap-1 text-[12px] font-medium text-warning">
            <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-warning" />
            needs you{meta?.runner_name ? ` · ${meta.runner_name}` : ''}
          </span>
        ) : liveWorking || answeredResuming ? (
          <span className="flex shrink-0 items-center gap-1 text-[12px] font-medium text-success">
            <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-success" />
            running{meta?.runner_name ? ` · ${meta.runner_name}` : ''}
          </span>
        ) : liveQueued ? (
          <span className="flex shrink-0 items-center gap-1 text-[12px] font-medium text-muted-foreground">
            <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-muted-foreground" />
            queued{meta?.runner_name ? ` · ${meta.runner_name}` : ''}
          </span>
        ) : staleRunning ? (
          <span className="flex shrink-0 items-center gap-1 text-[12px] font-medium text-success">
            <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-success" />
            running{meta?.runner_name ? ` · ${meta.runner_name}` : ''}
          </span>
        ) : meta?.runner_name ? (
          <span className="shrink-0 text-[12px] text-muted-foreground">
            idle · {meta.runner_name}
          </span>
        ) : meta?.requested_runner_name ? (
          <span className="shrink-0 text-[12px] text-muted-foreground" data-testid="chat-requested-runner">
            starts on {meta.requested_runner_name} with your first message
          </span>
        ) : null}
        {metaError && <span className="text-xs text-muted-foreground">· {metaError}</span>}
        <div className="ml-auto flex shrink-0 items-center gap-2">
          {actions.closeNote && <span className="text-[12px] text-muted-foreground">{actions.closeNote}</span>}
          {actions.resetNote && <span className="text-[12px] text-muted-foreground">{actions.resetNote}</span>}
          {actions.slackNote && <span className="text-[12px] text-muted-foreground">{actions.slackNote}</span>}
          {id && (
            <ChatSessionMenu
              sessionId={id}
              agentSlug={meta?.agent_slug ?? null}
              myRole={meta?.my_role ?? null}
              canAddPeople={meta?.can_add_people ?? false}
              notifyEveryCompletion={meta ? meta.notify_every_completion : undefined}
              onToggleNotify={() => void actions.toggleNotify()}
              shareDisabledReason={meta?.sensitive ? SENSITIVE_NO_SHARE : disabledReason}
              onShare={async (command) => {
                // Sent over REST like any message; `noteLocalSend` is what shows
                // the line and the working state until the transcript echoes it.
                // The SAME id rides both calls, so `QueuedRows.hideClientIds`
                // recognises the queued-turn projection as this row rather than
                // rendering the command a second time as an unowned placeholder.
                const clientId = `share-${Date.now()}`
                await sendMessage(id, command, clientId)
                socket.noteLocalSend(command, clientId)
              }}
              showLoadAll={paging.hasMoreBefore}
              onLoadAll={() => void paging.loadAll()}
              loadingAll={paging.loadingAll}
              showLoadFull={showLoadFull}
              onLoadFull={() => void paging.loadFull()}
              loadingFull={paging.loadingFull}
              onReset={() => void actions.resetFromTranscript()}
              resetting={actions.resetting}
              onClose={() => void actions.closeThisSession()}
              closing={actions.closing}
              // A viewer was given this chat to read, not to type into — the
              // control changes what a person's OWN composer shows others,
              // which a viewer has no composer for.
              slackThread={meta?.slack ?? null}
              // Same gate as the server's: an owner or editor of the session.
              onDetachSlack={canDetachSlack ? () => void actions.detachSlack() : undefined}
              detachingSlack={actions.detachingSlack}
              typingVisibility={canEditTypingVisibility ? socket.typingVisibility : undefined}
              onTypingVisibilityChange={
                canEditTypingVisibility ? socket.setTypingVisibility : undefined
              }
              workspace={meta?.workspace}
              moveTargets={actions.canMove ? actions.moveTargets : undefined}
              onMove={actions.canMove ? actions.moveToWorkspace : undefined}
              // Memory is off in a private conversation (the server refuses it too).
              memory={meta?.sensitive ? null : memory}
            />
          )}
        </div>
      </div>
      <SessionAudienceLine audience={meta?.audience} href={meta?.slack?.app_url || undefined} className="px-4 pb-1" />
      {meta?.sensitive && <PrivateConversationNote />}
      {meta?.sensitive && (
        <JoinRequests sessionId={id} actions={joinActions} live={liveJoinRequests}
          canDecide={meta.my_role === 'owner'} unlockElement={personTrust.element} />
      )}
      <SealedConversationBanner
        state={sealed.state}
        error={sealed.error}
        warning={sealed.warning}
        onDismissWarning={sealed.dismissWarning}
        onUnlock={() => void sealed.unlock()}
        unlockElement={vault.element}
        hasSealed={sealed.hasSealed}
        // The Runner app cannot use a passkey to unlock: read it in the browser.
        openElsewhere={passkeysWorkHere() ? undefined : (
          <button type="button" className="text-primary underline" onClick={() => openInBrowser(window.location.href)}>
            Read it in your browser
          </button>
        )}
        onSetAside={sealed.copyBroken ? () => {
          void setAsideMyKeyCopy(id)
            .then(() => getSession(id))
            .then((m) => setMeta((prev) => (prev ? { ...prev, sealed_key: m.sealed_key } : prev)))
            .catch(() => undefined)
        } : undefined}
      />
      <div className="min-h-0 flex-1" onContextMenu={fork.onMessageContextMenu}>
        {fork.messageMenu && (
          <MessageContextMenu
            x={fork.messageMenu.x}
            y={fork.messageMenu.y}
            busy={fork.forking}
            error={fork.forkError}
            copyText={fork.messageMenu.copyText}
            canFork={fork.messageMenu.canFork}
            onFork={fork.forkHere}
            onDismiss={fork.closeMessageMenu}
          />
        )}
        <ChatPanel
          state={panelState}
          connected={socket.connected}
          currentUserId={socket.state.current_user_id}
          onSend={(body) => {
            // The server consumes whatever the session is holding, so the chips
            // are spent the moment we send — leaving them would imply they will
            // ride along again on the next message.
            clearAttachments()
            socket.sendChat(body)
          }}
          onStop={socket.stopChat}
          onUnqueue={socket.unqueueChat}
          awaitingReply={socket.awaitingReply}
          attachments={attachments}
          onAttach={attach}
          onRemoveAttachment={removeAttachment}
          onUpdateDraft={socket.updateDraft}
          onDiscard={socket.discardDraft}
          // "Others see" moved into the ⋯ menu (ChatSessionMenu, above) — not
          // passed here, so the composer control disappears.
          renderMarkdown={renderChatMarkdown}
          // Keep a half-typed message across a route change or a closed tab.
          // Nothing else holds it: alone in a session the body is never
          // mirrored to the server until the moment you send.
          draftPersistKey={id}
          // MCP Apps: a Connected site's View for a tool result, acting as you.
          appHost={appHost}
          // The agent asking for a secret: the zero-knowledge seal card.
          secretRequestHost={secretRequestHost}
          // A private conversation's files, sealed: opened here with the conversation key.
          attachmentHost={attachmentHost}
          // The runner's model setting (opus[1m]) sizes the context meter's window.
          runnerModel={meta?.runner_model}
          slashCommands={slashCommands}
          // Copy is the kit's; Fork from here runs here (and opens the fork).
          messageActions={fork.messageActions}
          emptyState={emptyState}
          composerActions={<CanopyAIButton />}
          historySlot={historySlot}
          // Refuse the send outright rather than queueing it at a box that
          // cannot answer — the banner above holds the ways out.
          disabledReason={disabledReason}
          banner={
            visibleMenu ? (
              <MenuPrompt
                menu={visibleMenu}
                busy={menuAnswer.answering}
                error={menuAnswer.answerError}
                onAnswer={menuAnswer.answer}
              />
            ) : menuHidden ? (
              // The tap's receipt, shown only while the dialog is still being
              // reported. Once it is retracted the header saying "running" is
              // the whole story.
              <div className="rounded-md border border-border bg-muted/40 px-3 py-2 text-[12px] text-muted-foreground">
                Answer sent. The agent is picking it up.
              </div>
            ) : boundOffline ? (
              <PlacementBanner
                runnerName={meta?.runner_name ?? ''}
                eligibleRunners={placement.placementRunners}
                busy={placement.placing}
                error={placement.placeError}
                info={placement.placeInfo}
                onWait={placement.waitForIt}
                onPlace={placement.continueOn}
                paused={boundPaused}
                pausedNote={boundRunner?.paused_note || undefined}
                // Pause/resume is owner-only server-side; `can_manage` is the
                // row saying so. Without it, Resume would 404 and read as the
                // runner refusing to come back.
                onResume={boundPaused && boundRunner?.can_manage ? placement.resumeBoundRunner : undefined}
              />
            ) : undefined
          }
        />
      </div>
    </div>
  )
}

export default ChatPage
