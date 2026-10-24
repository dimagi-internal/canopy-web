import { useEffect, useMemo, useState, type ReactNode } from "react";

import type { PeerDraft, SessionState, SubagentSummary, TypingVisibility } from "./protocol";
import type { RenderMarkdown } from "./MessageItem";
import { ConnectionStatus } from "./ConnectionStatus";
import { MessageList } from "./MessageList";
import { PresenceChips } from "./PresenceChips";
import { isMine, isUnconfirmed } from "./identity";
import { QueuedRows } from "./QueuedRows";
import { PeerComposers } from "./PeerComposers";
import { SendBox, type PendingAttachment } from "./SendBox";
import type { SlashCommand } from "./slashCommands";
import { type DraftStorage } from "./drafts";
import { agentHasFloor as computeAgentHasFloor, pendingLabel as computePendingLabel, turnNotice } from "./turnStatus";
import { useStickyBottom } from "./useStickyBottom";
import { AppHostContext, type AppHost } from "./mcpApps/context";
import { SecretRequestHostContext, type SecretRequestHost } from "./secretRequest";
import { AttachmentHostContext, type AttachmentHost } from "./attachmentHost";
import { SubagentsContext } from "./subagents";

export interface ChatPanelProps {
  state: SessionState;
  connected: boolean;
  /** The viewer's user id — null for a contact (a widget visitor), who is
   *  identified by `state.current_contact_id` instead. */
  currentUserId: number | null;
  /** Called with the composer's text — pass it on to `sendChat`. */
  onSend: (body: string) => void;
  onStop: (messageId: string | null) => void;
  /** Withdraw one of your own still-queued sends (`useSessionSocket().unqueueChat`).
   *  Omit and queued rows offer no Cancel. */
  onUnqueue?: (turnId: string) => void;
  /** A send is outstanding but no reply has begun — the turn is queued,
   *  waiting for a runner to claim it. Keeps Stop reachable in that window. */
  awaitingReply?: boolean;
  /** Files staged for the next send; omit to hide attaching entirely. */
  attachments?: PendingAttachment[];
  onAttach?: (files: File[]) => void;
  onRemoveAttachment?: (id: string) => void;
  onUpdateDraft: (body: string) => void;
  /** @deprecated ignored since 0.13 — everyone has their own draft now, so
   *  there is no lock left to take over. Kept optional so a host still
   *  wiring `useSessionSocket().takeOverDraft` through (e.g. ace-web) keeps
   *  compiling across the upgrade. */
  onTakeOver?: () => void;
  onDiscard: () => void;
  renderMarkdown?: RenderMarkdown;
  /** Optional banner rendered above the composer. */
  banner?: ReactNode;
  /** Rendered when there are no messages yet. */
  emptyState?: ReactNode;
  /** When set, sending is disabled and this reason is shown. */
  disabledReason?: string;
  /** Rendered at the top of the scroll container (e.g. a "Load earlier" button / offline banner). */
  historySlot?: ReactNode;
  /** Persist the half-typed composer body under this key (the session id) so
   *  it survives routing away and back. Omit for in-memory-only drafts. */
  draftPersistKey?: string;
  /** Storage backing `draftPersistKey`; defaults to localStorage. */
  draftStorage?: DraftStorage | null;
  /** This person's chosen typing-visibility mode, and its setter. Both
   *  optional; the control renders only when both are given (see SendBox) —
   *  a host with no draft sync (a contact) passes neither. */
  typingVisibility?: TypingVisibility;
  onTypingVisibilityChange?: (visibility: TypingVisibility) => void;
  /** MCP Apps: how this page reaches canopy for the session's Views. Omit and
   *  a tool result with a View renders as an ordinary tool row. */
  appHost?: AppHost;
  /** How this page shows an agent's request for one of its secrets (a seal
   *  card). Omit and such a row renders as an ordinary tool row. */
  secretRequestHost?: SecretRequestHost;
  /** How this page shows the files sent with a person's line (a private
   *  conversation's are sealed). Omit and a line shows only its words. */
  attachmentHost?: AttachmentHost;
  /** Commands the composer offers when a message starts with `/`. */
  slashCommands?: readonly SlashCommand[];
}

/**
 * Presentational, app-agnostic chat surface: connection chip + presence,
 * a sticky-bottom message list, and the composer. Props-in / callbacks-out —
 * NO data fetching, NO WebSocket, NO CLI-auth. The container (e.g. canopy's
 * ChatPage) wires `useSessionSocket` returns into these props.
 */
const EMPTY_PEERS: PeerDraft[] = [];
const NO_SUBAGENTS: SubagentSummary[] = [];

export function ChatPanel({
  state,
  connected,
  currentUserId,
  onSend,
  onStop,
  onUnqueue,
  awaitingReply = false,
  attachments,
  onAttach,
  onRemoveAttachment,
  onUpdateDraft,
  onDiscard,
  renderMarkdown,
  banner,
  emptyState,
  disabledReason,
  historySlot,
  draftPersistKey,
  draftStorage,
  typingVisibility,
  onTypingVisibilityChange,
  appHost,
  secretRequestHost,
  attachmentHost,
  slashCommands,
}: ChatPanelProps) {
  // `onDiscard` is part of the public surface (co-edit teardown) even though
  // the default composer doesn't render a discard button. Referenced to keep
  // it wired without an unused-var error; a future toolbar can surface it.
  void onDiscard;

  // A turn is "in flight" from the moment the assistant row appears
  // (status=pending/streaming) until chat.stream_complete flips it to
  // complete. Treat pending AND streaming as in-flight so the Stop button
  // stays reachable during the "waiting for first token" window and SendBox
  // knows to show its "sent after the current reply" placeholder — Send
  // itself is never locked out by this any more (0.13): a send made while the
  // agent is replying is QUEUED, not blocked, and <QueuedRows> shows it landed.
  const inFlightMessage = useMemo(
    () =>
      state.messages.find(
        (m) => m.status === "streaming" || m.status === "pending",
      ) ?? null,
    [state.messages],
  );

  // Feedback where the eye actually is. Pressing send used to change NOTHING in
  // the transcript: no assistant row exists until `chat.stream_start`, which the
  // server emits together with `stream_complete` from a single `assistant`
  // ledger event — i.e. only once the reply's first TEXT exists, seconds to
  // minutes later. So MessageItem's own "Thinking…" treatment was unreachable on
  // the real runner path, and the only signal was a 12px chip in the header,
  // which on a phone is the far corner of the screen from your thumb.
  //
  // Three sources now, in `turnStatus.ts` — see `agentHasFloor` there for the
  // order of authority. The server's `turn_status` wins when present because
  // it is the only one that can say a turn is queued behind an offline box or
  // routed nowhere at all; `awaitingReply` still covers the instant between
  // pressing send and the first frame, which nothing server-side can.
  const status = state.turn_status ?? null;
  const agentHasFloor = computeAgentHasFloor({
    status,
    activity: state.activity,
    awaitingReply,
  });
  const showPendingReply = inFlightMessage == null && agentHasFloor;
  const pendingLabel = computePendingLabel({ status, activity: state.activity });
  // The sentence for an ask that is going NOWHERE. Rendered instead of the
  // bubble, never beside it: a spinner over a turn nothing will pick up is the
  // precise lie this projection exists to remove.
  //
  // Suppressed entirely when the host supplies its own `banner`. A host banner
  // IS this host's richer answer for the same situation — canopy's chat page
  // raises <PlacementBanner> for an offline runner, with buttons to wait or
  // continue elsewhere, and <MenuPrompt> for a dialog. Rendering our sentence
  // underneath would state the same fact twice and put a dead-end restatement
  // directly below the thing you can actually press. The fallback is for hosts
  // with no banner of their own, which is exactly the embedded widget.
  const latestMessageAt =
    state.messages.length > 0 ? state.messages[state.messages.length - 1].created_at : null;
  // A clock only while a turn is working: the quiet notice is about time passing
  // with NOTHING arriving, so no frame would ever re-render it.
  const now = useNow(status?.state === "working" ? 30_000 : null);
  // A reply streaming in right now is something coming back, however old its row.
  const notice = banner ? null : turnNotice(status, {
    latestMessageAt: inFlightMessage ? new Date(now).toISOString() : latestMessageAt,
    now,
  });

  // Sticky-bottom scroll: dep changes on (a) new message arrival and (b)
  // streaming text growth on the last message. length-only (cheap) instead
  // of the full string so the effect doesn't re-run on equal characters.
  // `showPendingReply` is in the dep too — the bubble is a new row at the
  // bottom, and appearing below the fold would defeat the whole point of it.
  const messages = state.messages;
  const lastMessageLen =
    messages.length > 0 ? messages[messages.length - 1].plaintext.length : 0;
  const queuedCount = state.queued?.length ?? 0;
  const scrollDep = `${messages.length}:${lastMessageLen}:${showPendingReply}:${queuedCount}`;
  const { containerRef, onScroll } = useStickyBottom(scrollDep);

  // A send still waiting its turn is drawn ONCE, in the queue, in the order the
  // server took it. Your own optimistic row for a send the server lists as
  // `queued` leaves the transcript and is drawn by <QueuedRows> instead: left
  // in place it read as already sent, and it sat ABOVE every queued row without
  // a bubble of its own (a send from before a remount), so a later line showed
  // first. A `delivering` send keeps its bubble — it is the agent's next read.
  const waitingClientIds = useMemo(
    () =>
      new Set(
        (state.queued ?? [])
          .filter((q) => q.state === "queued" && q.client_id)
          .map((q) => q.client_id),
      ),
    [state.queued],
  );
  const shownMessages = useMemo(
    () =>
      waitingClientIds.size === 0
        ? state.messages
        : state.messages.filter(
            (m) =>
              !(
                m.role === "user" &&
                isUnconfirmed(m) &&
                typeof m.content?.client_id === "string" &&
                waitingClientIds.has(m.content.client_id)
              ),
          ),
    [state.messages, waitingClientIds],
  );

  // Every other send of yours already renders in the transcript (as a
  // "pending" user row keyed by client_id) the instant you press send, so a
  // queued row for the SAME send would show it twice — once as your bubble,
  // once as a dashed "queued" placeholder. hideClientIds is every client_id
  // still on screen in the transcript.
  const hideClientIds = useMemo(
    () =>
      new Set(
        shownMessages
          .map((m) => m.content?.client_id)
          .filter((c): c is string => typeof c === "string"),
      ),
    [shownMessages],
  );

  // Who is looking, for "is this line mine": a member by user id, a contact
  // (the widget) by the snapshot's contact id.
  const currentContactId = state.current_contact_id ?? null;

  // The text of your own sends still waiting to be confirmed — for a client
  // that sends no client_id (an older host's HTTP send), the only way to tell
  // its queued entry from a second copy of the same line.
  // Your own sends a runner has not picked up yet, by client_id: the row your
  // optimistic echo draws is where their Cancel goes.
  const cancellable = useMemo(
    () =>
      new Map(
        (state.queued ?? [])
          .filter((q) => q.state === "queued" && q.client_id && isMine(q.author, currentUserId, currentContactId))
          .map((q) => [q.client_id, q.turn_id] as const),
      ),
    [state.queued, currentUserId, currentContactId],
  );
  const ownPendingTexts = useMemo(
    () =>
      new Set(
        shownMessages
          .filter(
            (m) =>
              m.role === "user" &&
              (m.status === "pending" || m.id.startsWith("local:")) &&
              (!m.author || isMine(m.author, currentUserId, currentContactId)),
          )
          .map((m) => m.plaintext.trim()),
      ),
    [shownMessages, currentUserId, currentContactId],
  );

  // Everyone else's live drafts. The same list draws their boxes above the
  // composer and marks their chips in the header, so the two never disagree.
  const peerDrafts = state.peer_drafts ?? EMPTY_PEERS;
  const typingUserIds = useMemo(() => peerDrafts.map((p) => p.author.id), [peerDrafts]);

  return (
    <AppHostContext.Provider value={appHost ?? null}>
    <SecretRequestHostContext.Provider value={secretRequestHost ?? null}>
    <AttachmentHostContext.Provider value={attachmentHost ?? null}>
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-3 border-b border-border bg-background px-3 py-1.5 text-xs">
        <ConnectionStatus connected={connected} />
        <div className="ml-auto">
          <PresenceChips
            participants={state.participants}
            presenceUserIds={state.presence_user_ids}
            currentUserId={currentUserId}
            typingUserIds={typingUserIds}
          />
        </div>
      </div>
      {/* Pinned ABOVE the scroll container, not inside it. The container is
          auto-scrolled to the bottom (useStickyBottom), so a slot rendered as
          the first child of the scroll content sits above the visible area —
          on prod the "Load full session" control on an empty runner-discovered
          session was in the DOM but scrolled out of view and covered by the
          header, i.e. unreachable. Pinning keeps "Load earlier"/"Load full"
          always visible. */}
      {historySlot}
      {/* overflow-x-hidden: `overflow-y: auto` alone computes overflow-x to
          auto, so anything wider than the column made the transcript scroll
          sideways into empty space. Wide content scrolls inside its own box. */}
      <div ref={containerRef} onScroll={onScroll} className="flex-1 overflow-x-hidden overflow-y-auto">
        <SubagentsContext.Provider value={state.subagents ?? NO_SUBAGENTS}>
          <MessageList
            messages={shownMessages}
            emptyState={emptyState}
            renderMarkdown={renderMarkdown}
            pendingReply={showPendingReply}
            pendingLabel={pendingLabel}
            currentUserId={currentUserId}
            currentContactId={currentContactId}
            cancellable={cancellable}
            onCancelQueued={onUnqueue}
          />
        </SubagentsContext.Provider>
        <QueuedRows
          queued={state.queued ?? []}
          hideClientIds={hideClientIds}
          currentUserId={currentUserId}
          currentContactId={currentContactId}
          ownPendingTexts={ownPendingTexts}
          onCancel={onUnqueue}
        />
      </div>
      <SendBox
        draft={state.active_draft}
        connected={connected}
        // The same answer the "Thinking…" bubble and the header give. Gating Stop
        // (and Esc) on a streaming ROW alone hid it for most of a runner turn:
        // that row only exists once the reply's first text does, so a turn busy
        // in tools had a bubble saying it was working and no way to stop it.
        isStreaming={inFlightMessage != null || awaitingReply || agentHasFloor}
        streamingMessageId={inFlightMessage?.id ?? null}
        // "After the current reply" is false while nothing is running: the ask
        // ahead is stuck, so a new message waits behind it.
        waitingPlaceholder={
          status?.stuck && !status.menu_pending && !status.settled
            ? "Type a message — it waits in the queue behind the one above"
            : null
        }
        onUpdate={onUpdateDraft}
        onSend={onSend}
        onStop={onStop}
        slashCommands={slashCommands}
        stopState={state.stopState}
        peers={<PeerComposers peers={peerDrafts} />}
        banner={
          notice ? (
            <p
              role="status"
              className={
                notice.tone === "error"
                  ? "text-[12px] text-destructive"
                  : "text-[12px] text-warning"
              }
            >
              {notice.text}
              {notice.offer ? ` A runner admin can send it to ${notice.offer.name}.` : ""}
            </p>
          ) : (
            banner
          )
        }
        disabledReason={disabledReason}
        attachments={attachments}
        onAttach={onAttach}
        onRemoveAttachment={onRemoveAttachment}
        persistKey={draftPersistKey}
        storage={draftStorage}
        typingVisibility={typingVisibility}
        onTypingVisibilityChange={onTypingVisibilityChange}
      />
    </div>
    </AttachmentHostContext.Provider>
    </SecretRequestHostContext.Provider>
    </AppHostContext.Provider>
  );
}


/** `Date.now()`, re-read every `everyMs` — or frozen when `everyMs` is null. */
function useNow(everyMs: number | null): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (everyMs == null) return;
    setNow(Date.now());
    const id = setInterval(() => setNow(Date.now()), everyMs);
    return () => clearInterval(id);
  }, [everyMs]);
  return now;
}
