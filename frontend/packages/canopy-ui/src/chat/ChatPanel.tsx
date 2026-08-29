import { useEffect, useMemo, useState, type ReactNode } from "react";

import type { SessionState } from "./protocol";
import type { RenderMarkdown } from "./MessageItem";
import { ConnectionStatus } from "./ConnectionStatus";
import { MessageList } from "./MessageList";
import { PresenceChips } from "./PresenceChips";
import { SendBox, type PendingAttachment } from "./SendBox";
import { isDraftIdle, msUntilDraftIdle, type DraftStorage } from "./drafts";
import { useStickyBottom } from "./useStickyBottom";

export interface ChatPanelProps {
  state: SessionState;
  connected: boolean;
  currentUserId: number;
  onSend: () => void;
  onStop: (messageId: string | null) => void;
  /** A send is outstanding but no reply has begun — the turn is queued,
   *  waiting for a runner to claim it. Keeps Stop reachable in that window. */
  awaitingReply?: boolean;
  /** Files staged for the next send; omit to hide attaching entirely. */
  attachments?: PendingAttachment[];
  onAttach?: (files: File[]) => void;
  onRemoveAttachment?: (id: string) => void;
  onUpdateDraft: (body: string) => void;
  onTakeOver: () => void;
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
}

/**
 * Presentational, app-agnostic chat surface: connection chip + presence,
 * a sticky-bottom message list, and the composer. Props-in / callbacks-out —
 * NO data fetching, NO WebSocket, NO CLI-auth. The container (e.g. canopy's
 * ChatPage) wires `useSessionSocket` returns into these props.
 */
export function ChatPanel({
  state,
  connected,
  currentUserId,
  onSend,
  onStop,
  awaitingReply = false,
  attachments,
  onAttach,
  onRemoveAttachment,
  onUpdateDraft,
  onTakeOver,
  onDiscard,
  renderMarkdown,
  banner,
  emptyState,
  disabledReason,
  historySlot,
  draftPersistKey,
  draftStorage,
}: ChatPanelProps) {
  // `onDiscard` is part of the public surface (co-edit teardown) even though
  // the default composer doesn't render a discard button. Referenced to keep
  // it wired without an unused-var error; a future toolbar can surface it.
  void onDiscard;

  // Force a re-render when the draft lock transitions from live to idle so
  // PresenceChips' amber-highlight updates at T+2s without waiting for some
  // unrelated event to arrive.
  const [, forceIdleTick] = useState(0);
  useEffect(() => {
    const draft = state.active_draft;
    if (!draft) return;
    const remaining = msUntilDraftIdle(draft);
    if (remaining === 0) return;
    const t = window.setTimeout(() => forceIdleTick((n) => n + 1), remaining + 10);
    return () => window.clearTimeout(t);
  }, [state.active_draft?.last_edit_at, state.active_draft]);

  const holderId = state.active_draft?.last_editor ?? null;
  const holderIsPresent =
    holderId != null && state.presence_user_ids.includes(holderId);

  // A turn is "in flight" from the moment the assistant row appears
  // (status=pending/streaming) until chat.stream_complete flips it to
  // complete. Treat pending AND streaming as in-flight so the send button
  // stays locked out and the stop button is reachable during the "waiting
  // for first token" window.
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
  // Two sources, deliberately: `awaitingReply` is client-side and answers
  // INSTANTLY with no round trip (nothing server-side can — the turn has to be
  // enqueued, claimed and driven into the agent before anything could report),
  // and `activity === "working"` keeps it up for the rest of the turn, across
  // the gaps between the assistant's separate text blocks. `blocked` withdraws
  // it: an agent waiting on YOU must never render as an agent working.
  const agentHasFloor =
    state.activity !== "blocked" &&
    (awaitingReply || state.activity === "working");
  const showPendingReply = inFlightMessage == null && agentHasFloor;
  // "Queued" until something reports the agent actually started — the useful
  // distinction is where the delay is, not that there is one.
  const pendingLabel = state.activity === "working" ? "Thinking…" : "Queued…";

  // Sticky-bottom scroll: dep changes on (a) new message arrival and (b)
  // streaming text growth on the last message. length-only (cheap) instead
  // of the full string so the effect doesn't re-run on equal characters.
  // `showPendingReply` is in the dep too — the bubble is a new row at the
  // bottom, and appearing below the fold would defeat the whole point of it.
  const messages = state.messages;
  const lastMessageLen =
    messages.length > 0 ? messages[messages.length - 1].plaintext.length : 0;
  const scrollDep = `${messages.length}:${lastMessageLen}:${showPendingReply}`;
  const { containerRef, onScroll } = useStickyBottom(scrollDep);

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-3 border-b border-border bg-background px-3 py-1.5 text-xs">
        <ConnectionStatus connected={connected} />
        <div className="ml-auto">
          <PresenceChips
            participants={state.participants}
            presenceUserIds={state.presence_user_ids}
            draftHolderId={holderId}
            draftHolderIdle={isDraftIdle(state.active_draft)}
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
      <div ref={containerRef} onScroll={onScroll} className="flex-1 overflow-y-auto">
        <MessageList
          messages={state.messages}
          emptyState={emptyState}
          renderMarkdown={renderMarkdown}
          pendingReply={showPendingReply}
          pendingLabel={pendingLabel}
        />
      </div>
      <SendBox
        draft={state.active_draft}
        connected={connected}
        currentUserId={currentUserId}
        holderIsPresent={holderIsPresent}
        isStreaming={inFlightMessage != null || awaitingReply}
        streamingMessageId={inFlightMessage?.id ?? null}
        onUpdate={onUpdateDraft}
        onSend={onSend}
        onStop={onStop}
        stopState={state.stopState}
        onTakeOver={onTakeOver}
        banner={banner}
        disabledReason={disabledReason}
        attachments={attachments}
        onAttach={onAttach}
        onRemoveAttachment={onRemoveAttachment}
        persistKey={draftPersistKey}
        storage={draftStorage}
      />
    </div>
  );
}
