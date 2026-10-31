import { useCallback, useMemo, useState, type KeyboardEvent, type ReactNode } from "react";
import { ArrowDown, Search } from "lucide-react";

import type {
  Message, PeerDraft, QueuedMessage, SessionState, SubagentSummary, TypingVisibility,
} from "./protocol";
import type { RenderMarkdown } from "./MessageItem";
import { ConnectionStatus } from "./ConnectionStatus";
import { MessageList, type ToolRowsView } from "./MessageList";
import { PresenceChips } from "./PresenceChips";
import { withCancelledSends } from "./cancelledSends";
import { isMine } from "./identity";
import { isCompactionSummary } from "./contextUsage";
import { placeSends } from "./placeSends";
import { useTurnView } from "./turnView";
import { QueuedRows } from "./QueuedRows";
import { RunningAgents } from "./RunningAgents";
import { PeerComposers } from "./PeerComposers";
import { SendBox, type PendingAttachment } from "./SendBox";
import type { SlashCommand } from "./slashCommands";
import { type DraftStorage } from "./drafts";
import { composerPlaceholder, pendingLabel as computePendingLabel, turnNotice } from "./turnStatus";
import { ContextMeter } from "./ContextMeter";
import { useStickyBottom } from "./useStickyBottom";
import { useNow } from "./useNow";
import { FindBar } from "./FindBar";
import { AppHostContext, type AppHost } from "./mcpApps/context";
import { SecretRequestHostContext, type SecretRequestHost } from "./secretRequest";
import { AttachmentHostContext, type AttachmentHost } from "./attachmentHost";
import { SubagentsContext } from "./subagents";
import { MessageActionsContext, type MessageActionsHost } from "./messageActions";

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
  /** The person answered the agent's dialog here, so the turn reads as
   *  resuming rather than blocked (`turnView`). Pass the same value to the
   *  header's `useTurnView` so the two agree. */
  pendingAnswer?: boolean;
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
  /** @deprecated never called: the panel has no discard control. Kept
   *  optional so a host still passing `useSessionSocket().discardDraft`
   *  keeps compiling. */
  onDiscard?: () => void;
  renderMarkdown?: RenderMarkdown;
  /** Optional banner rendered above the composer. */
  banner?: ReactNode;
  /** Rendered when there are no messages yet. */
  emptyState?: ReactNode;
  /** When set, sending is disabled and this reason is shown. */
  disabledReason?: string;
  /** Rendered at the top of the scroll container (e.g. a "Load earlier" button / offline banner). */
  historySlot?: ReactNode;
  /** The host's own chips beside the connection chip in the header (e.g. the Slack thread). */
  statusSlot?: ReactNode;
  /** The host's own controls beside Send (`SendBox.actions`). */
  composerActions?: ReactNode;
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
  /** The bound runner's Claude model setting (`opus[1m]`), which sizes the
   *  context meter's window. Omit and the window is inferred or "unknown". */
  runnerModel?: string | null;
  /** Per-message actions the host runs (Fork from here). Copy is always on;
   *  Edit and resend is the panel's own, offered on your last line. */
  messageActions?: MessageActionsHost;
  /** Open or close every tool row at once (see `MessageList.toolRows`). */
  toolRows?: ToolRowsView;
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
  pendingAnswer = false,
  attachments,
  onAttach,
  onRemoveAttachment,
  onUpdateDraft,
  renderMarkdown,
  banner,
  emptyState,
  disabledReason,
  historySlot,
  statusSlot,
  composerActions,
  draftPersistKey,
  draftStorage,
  typingVisibility,
  onTypingVisibilityChange,
  appHost,
  secretRequestHost,
  attachmentHost,
  slashCommands,
  runnerModel,
  messageActions,
  toolRows,
}: ChatPanelProps) {
  // ONE answer to "is the last turn live" (`turnView`) — the same the host's
  // header reads. The activity line, Stop, the composer's placeholder and the
  // transcript's fold all derive from it.
  const status = state.turn_status ?? null;
  const view = useTurnView({ state, awaitingReply, pendingAnswer });
  const { phase, inFlight: inFlightMessage, working: isStreaming } = view;
  const agentHasFloor = phase === "working" || phase === "queued";
  // The live line (what it is doing, the clock, Stop) stays under the turn
  // until it ends — while the reply streams too. It is the chat's one Stop
  // button; the composer has only Esc.
  const showActivityLine = isStreaming;
  const pendingLabel = computePendingLabel({ status, activity: state.activity });
  // The sentence for an ask that is going NOWHERE, rendered instead of the
  // activity line, never beside it.
  //
  // Suppressed when the host supplies its own `banner`: that IS the host's
  // richer answer for the same situation (canopy's <PlacementBanner> for an
  // offline runner, <MenuPrompt> for a dialog), and stating the fact twice puts
  // a dead-end restatement under the thing you can press. The fallback is for
  // hosts with no banner of their own, such as the embedded widget.
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

  // "Something new arrived" for the sticky-bottom scroll: a row was added or
  // the newest row is a different one. Keyed by the row's stable identity (a
  // send's client id survives its receipt), so a send being confirmed or the
  // activity line appearing is not news. Growth within a row — a reply
  // streaming in — is followed by the hook's ResizeObserver.
  const messages = state.messages;
  const last = messages.length > 0 ? messages[messages.length - 1] : null;
  const lastIdentity = last
    ? typeof last.content?.client_id === "string" ? last.content.client_id : last.id
    : "";
  const scrollDep = `${messages.length}:${lastIdentity}`;
  const { containerRef, onScroll, onWheel, scrollToBottom, away, unseen } = useStickyBottom(scrollDep);

  // Queued vs a runner has it.
  const pendingWorking = phase === "working";
  const canStop = view.canStop && !disabledReason;

  // ONE way to stop, whichever control asked — Esc in the composer or Stop on
  // the turn's live line — and one "stopping…" both of them show. A stop you
  // asked for shows AT ONCE, before the server says anything, and is held
  // until the server's verdict arrives or the turn ends.
  const [stopAsked, setStopAsked] = useState(false);
  const [stopVerdict, setStopVerdict] = useState(state.stopState);
  if (stopVerdict !== state.stopState) {
    setStopVerdict(state.stopState);
    if (state.stopState !== undefined && state.stopState !== "requested") setStopAsked(false);
  }
  const [wasStreaming, setWasStreaming] = useState(isStreaming);
  if (wasStreaming !== isStreaming) {
    setWasStreaming(isStreaming);
    if (!isStreaming && stopAsked) setStopAsked(false);
  }
  const stopping = state.stopState === "requested" || (stopAsked && state.stopState === undefined);
  const requestStop = useCallback(
    (messageId: string | null) => {
      setStopAsked(true);
      onStop(messageId);
    },
    [onStop],
  );
  const stopTurn = useCallback(
    () => requestStop(inFlightMessage?.id ?? null),
    [requestStop, inFlightMessage],
  );

  // A running agent's own row, from the strip above the composer.
  const showAgentRow = useCallback(
    (toolUseId: string) => {
      const row = Array.from(containerRef.current?.querySelectorAll("[data-tool-use-id]") ?? [])
        .find((el) => el.getAttribute("data-tool-use-id") === toolUseId);
      if (!row) return;
      // A background agent outlives its turn, whose work then folds away: open
      // whatever it is folded into, or there is nothing to scroll to.
      for (let el = row.parentElement?.closest("details"); el; el = el.parentElement?.closest("details")) {
        el.open = true;
      }
      row.scrollIntoView({ block: "center", behavior: "smooth" });
    },
    [containerRef],
  );

  // Find in the loaded transcript: Cmd/Ctrl+F while the chat has focus.
  const [finding, setFinding] = useState(false);
  const onPanelKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if ((e.metaKey || e.ctrlKey) && !e.altKey && e.key.toLowerCase() === "f") {
      e.preventDefault();
      setFinding(true);
    }
  };

  // Who is looking, for "is this line mine": a member by user id, a contact
  // (the widget) by the snapshot's contact id.
  const currentContactId = state.current_contact_id ?? null;

  // Where each of your sends is drawn — as your bubble, or in the queue — in
  // one pure answer (`placeSends`).
  const placed = useMemo(
    () => placeSends(state.messages, state.queued, { userId: currentUserId, contactId: currentContactId }, status),
    [state.messages, state.queued, currentUserId, currentContactId, status],
  );
  const shownMessages = placed.shown;
  // What the list draws: the same, plus the sends withdrawn before the agent read
  // them, in the place they were sent. Kept apart from `shownMessages` because
  // everything else (the queue fold, Edit, "is a reply owed") reasons about real
  // lines, and a cancelled one is none of those.
  const displayMessages = useMemo(
    () => withCancelledSends(shownMessages, state.cancelled),
    [shownMessages, state.cancelled],
  );

  // Everyone else's live drafts. The same list draws their boxes above the
  // composer and marks their chips in the header, so the two never disagree.
  const peerDrafts = state.peer_drafts ?? EMPTY_PEERS;
  const typingUserIds = useMemo(() => peerDrafts.map((p) => p.author.id), [peerDrafts]);

  // Edit and resend, on your own last line only: while a runner has not taken it
  // (editing withdraws it from the queue), or once its turn is over. Either way
  // the text goes back in the box and sending it is a new turn — history is
  // never rewritten. Not offered while the agent is working on it.
  const [prefill, setPrefill] = useState<{ text: string; seq: number } | null>(null);
  const putBack = (text: string) => setPrefill((p) => ({ text, seq: (p?.seq ?? 0) + 1 }));
  const edit = useMemo(
    () =>
      editTarget({
        messages: shownMessages, queued: state.queued ?? [], currentUserId, currentContactId,
        canUnqueue: Boolean(onUnqueue),
        settled: inFlightMessage == null && !agentHasFloor,
        blocked: Boolean(disabledReason),
      }),
    [shownMessages, state.queued, currentUserId, currentContactId, onUnqueue, inFlightMessage,
     agentHasFloor, disabledReason],
  );
  const editQueued = (q: QueuedMessage) => {
    onUnqueue?.(q.turn_id);
    putBack(q.text);
  };
  const actionsValue = {
    host: messageActions ?? null,
    editableId: edit?.messageId ?? null,
    onEdit: edit
      ? (m: Message) => (edit.queued ? editQueued(edit.queued) : putBack(m.plaintext))
      : null,
  };

  return (
    <AppHostContext.Provider value={appHost ?? null}>
    <SecretRequestHostContext.Provider value={secretRequestHost ?? null}>
    <AttachmentHostContext.Provider value={attachmentHost ?? null}>
    <MessageActionsContext.Provider value={actionsValue}>
    <div className="flex h-full flex-col" onKeyDown={onPanelKeyDown}>
      <div className="flex items-center gap-3 border-b border-border bg-background px-3 py-1.5 text-xs">
        <ConnectionStatus connected={connected} />
        {statusSlot}
        <button
          type="button"
          onClick={() => setFinding((f) => !f)}
          aria-label="Find in loaded messages"
          aria-pressed={finding}
          title="Find in loaded messages (⌘F / Ctrl+F)"
          className="ml-auto rounded p-1 text-muted-foreground hover:bg-muted hover:text-foreground"
        >
          <Search className="h-3.5 w-3.5" />
        </button>
        <div className="flex items-center gap-3">
          <ContextMeter messages={state.messages} runnerModel={runnerModel} />
          <PresenceChips
            participants={state.participants}
            presenceUserIds={state.presence_user_ids}
            currentUserId={currentUserId}
            typingUserIds={typingUserIds}
          />
        </div>
      </div>
      {/* Pinned ABOVE the scroll container, not inside it: the container is
          auto-scrolled to the bottom (useStickyBottom), so a slot inside the
          scroll content would sit above the visible area, out of reach. */}
      {finding && (
        <FindBar rootRef={containerRef} version={scrollDep} onClose={() => setFinding(false)} />
      )}
      {historySlot}
      <div className="relative min-h-0 flex-1">
      {/* overflow-x-hidden: `overflow-y: auto` alone computes overflow-x to
          auto, so anything wider than the column made the transcript scroll
          sideways into empty space. Wide content scrolls inside its own box.
          tabIndex -1: clicking the transcript gives the chat focus, so ⌘F
          finds in it rather than in the whole page. */}
      <div
        ref={containerRef}
        onScroll={onScroll}
        onWheel={onWheel}
        tabIndex={-1}
        data-testid="chat-scroll"
        className="h-full overflow-x-hidden overflow-y-auto outline-none"
      >
        <SubagentsContext.Provider value={state.subagents ?? NO_SUBAGENTS}>
          <MessageList
            messages={displayMessages}
            emptyState={emptyState}
            renderMarkdown={renderMarkdown}
            pendingReply={showActivityLine}
            pendingLabel={pendingLabel}
            pendingWorking={pendingWorking || inFlightMessage?.status === "streaming"}
            pendingRunner={status?.claimed_by ?? null}
            pendingStartedAt={pendingWorking ? status?.claimed_at ?? null : null}
            onStopTurn={canStop ? stopTurn : undefined}
            stopping={stopping}
            liveTurn={view.listLive}
            currentUserId={currentUserId}
            currentContactId={currentContactId}
            cancellable={placed.cancellable}
            onCancelQueued={onUnqueue}
            toolRows={toolRows}
          />
        </SubagentsContext.Provider>
        <QueuedRows
          queued={state.queued ?? []}
          hideClientIds={placed.hideClientIds}
          currentUserId={currentUserId}
          currentContactId={currentContactId}
          ownPendingTexts={placed.ownPendingTexts}
          onCancel={onUnqueue}
          editableTurnId={edit?.queued?.turn_id ?? null}
          onEdit={editQueued}
        />
      </div>
      {away && (
        // Scrolled up to read, so new rows are not followed: say when there
        // are some, and offer the way back down either way.
        <button
          type="button"
          onClick={scrollToBottom}
          data-testid="jump-to-latest"
          className="absolute bottom-3 left-1/2 z-20 flex -translate-x-1/2 items-center gap-1 rounded-full border border-border bg-card px-3 py-1 text-xs font-medium text-foreground shadow-md hover:bg-muted"
        >
          <ArrowDown className="h-3 w-3" aria-hidden="true" />
          {unseen ? "New messages" : "Jump to latest"}
        </button>
      )}
      </div>
      <RunningAgents subagents={state.subagents ?? NO_SUBAGENTS} onShow={showAgentRow} />
      <SendBox
        draft={state.active_draft}
        connected={connected}
        isStreaming={isStreaming}
        streamingMessageId={inFlightMessage?.id ?? null}
        // What happens to a message sent now: typed into the running turn,
        // or waiting — and behind what (`composerPlaceholder`).
        waitingPlaceholder={composerPlaceholder(status)}
        onUpdate={onUpdateDraft}
        onSend={onSend}
        onStop={requestStop}
        slashCommands={slashCommands}
        stopState={state.stopState}
        stopping={stopping}
        actions={composerActions}
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
        prefill={prefill}
      />
    </div>
    </MessageActionsContext.Provider>
    </AttachmentHostContext.Provider>
    </SecretRequestHostContext.Provider>
    </AppHostContext.Provider>
  );
}


/** Which of your lines "Edit and resend" is offered on, if any: your most recent
 *  send while it is still queued and unclaimed, else your last line in the
 *  transcript once nothing is running. Null while a runner holds your send or the
 *  agent is working, and when sending is refused. */
export function editTarget({
  messages, queued, currentUserId, currentContactId, canUnqueue, settled, blocked,
}: {
  messages: Message[];
  queued: QueuedMessage[];
  currentUserId: number | null;
  currentContactId: number | null;
  canUnqueue: boolean;
  settled: boolean;
  blocked: boolean;
}): { messageId: string | null; queued: QueuedMessage | null } | null {
  if (blocked) return null;
  const mine = queued.filter((q) => isMine(q.author, currentUserId, currentContactId));
  const latest = mine[mine.length - 1];
  if (latest) {
    if (latest.state !== "queued" || !canUnqueue) return null;
    const row = latest.client_id
      ? messages.find((m) => m.role === "user" && m.content?.client_id === latest.client_id)
      : undefined;
    return { messageId: row?.id ?? null, queued: latest };
  }
  if (!settled) return null;
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    if (m.role !== "user" || m.content?.cancelled === true || isCompactionSummary(m)) {
      continue;
    }
    // A row with no author predates authorship, and renders as yours.
    if (m.author && !isMine(m.author, currentUserId, currentContactId)) continue;
    return m.plaintext.trim() ? { messageId: m.id, queued: null } : null;
  }
  return null;
}
