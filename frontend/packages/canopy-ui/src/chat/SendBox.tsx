import {
  useEffect,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from "react";

import type { Draft } from "./protocol";
import { isDraftIdle, msUntilDraftIdle } from "./drafts";
import { Button } from "../ui/button";

interface Props {
  draft: Draft | null;
  /** Live socket. Typing never depends on this; SENDING does — see canSend. */
  connected: boolean;
  currentUserId: number;
  holderIsPresent: boolean;
  isStreaming: boolean;
  streamingMessageId: string | null;
  onUpdate: (body: string) => void;
  onSend: () => void;
  onStop: (messageId: string) => void;
  onTakeOver: () => void;
  /** Optional app-supplied banner rendered above the composer (e.g. an
   *  imported-session note). The kit itself has no CLI-auth banners. */
  banner?: ReactNode;
  /** When set, sending is disabled and this reason is shown as a hint. */
  disabledReason?: string;
}

export function SendBox({
  draft,
  connected,
  currentUserId,
  holderIsPresent,
  isStreaming,
  streamingMessageId,
  onUpdate,
  onSend,
  onStop,
  onTakeOver,
  banner,
  disabledReason,
}: Props) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  // Force a re-render when the lock transitions from live to idle.
  // Without this, nothing would trigger a re-render exactly at T+2s
  // after the last edit, and another user's UI would stay locked
  // indefinitely until some unrelated event happens to arrive.
  const [, forceTick] = useState(0);

  useEffect(() => {
    if (!draft) return;
    const remaining = msUntilDraftIdle(draft);
    if (remaining === 0) return;
    const t = window.setTimeout(() => forceTick((n) => n + 1), remaining + 10);
    return () => window.clearTimeout(t);
  }, [draft?.last_edit_at, draft]);

  const holderId = draft?.last_editor ?? null;
  const isHolder = holderId != null && holderId === currentUserId;
  const holderIsIdle = isDraftIdle(draft);

  // LOCAL-FIRST: the textarea's value is local state, never server state.
  // Rendering `draft.body` directly made every inbound frame a chance to
  // overwrite the user mid-keystroke — a stale echo of your own debounced
  // update, or a `session.state` snapshot on reconnect (which replaces state
  // wholesale), would rewind the composer to a body from 150ms ago. In
  // single-player that reconciliation protects against nothing at all, since
  // there is no co-editor whose edits could be lost.
  const [localBody, setLocalBody] = useState(draft?.body ?? "");

  // The ONE case where the server genuinely knows better than this client:
  // somebody ELSE edited the shared draft. Our own echo is ignored, which is
  // also what stops two clients on one account (phone + desktop) from
  // fighting — each keeps its own text instead of clobbering the other.
  const theirEdit =
    draft != null && draft.last_editor !== currentUserId ? draft.body : null;
  useEffect(() => {
    if (theirEdit != null) setLocalBody(theirEdit);
  }, [theirEdit]);

  // Typing is ALWAYS allowed unless a teammate is actively holding the draft.
  // It used to require `draft != null`, so the composer was disabled until
  // session.state landed — locking you out of your own input on first paint
  // and again on every reconnect. Keystrokes typed early are held locally and
  // flushed when the draft exists (see useSessionSocket.sendChat).
  const canEdit = isHolder || holderIsIdle || !holderIsPresent;

  useEffect(() => {
    if (canEdit && !isHolder && textareaRef.current) {
      textareaRef.current.focus();
    }
  }, [canEdit, isHolder]);

  const body = localBody;
  const blocked = Boolean(disabledReason);
  // Sending needs a draft (`chat.send` commits the SERVER's copy, so there must
  // be one) AND a live socket. The socket check is load-bearing now that the
  // composer clears optimistically: `send()` drops every frame but chat.stop
  // when the socket is closed, so an allowed-but-undeliverable send would clear
  // the box and lose the message outright.
  const canSend =
    canEdit &&
    connected &&
    draft != null &&
    body.trim().length > 0 &&
    !isStreaming &&
    !blocked;

  const handleChange = (value: string) => {
    setLocalBody(value);
    onUpdate(value);
  };

  const handleSend = () => {
    // Clear locally rather than waiting for the server's cleared draft to
    // echo back: that echo carries last_editor === us, which the adopt rule
    // above (correctly) ignores, so nothing else would empty the box.
    setLocalBody("");
    onSend();
  };

  const handleKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    // `isComposing` is true during IME input (CJK, etc.). Pressing
    // Enter to commit a composition must not send the message.
    const isComposing = (e.nativeEvent as unknown as { isComposing?: boolean })
      .isComposing;
    if (e.key === "Enter" && !e.shiftKey && !isComposing) {
      e.preventDefault();
      if (canSend) handleSend();
    }
  };

  const handleStopClick = () => {
    if (streamingMessageId != null) onStop(streamingMessageId);
  };

  const placeholder = blocked
    ? disabledReason
    : !canEdit
      ? "Another teammate is editing…"
      : !draft
        ? "Type a message… (connecting…)"
        : "Type a message… (Enter to send, Shift+Enter for newline)";

  return (
    <div className="border-t border-border bg-background">
      {banner}
      <div className="p-2">
        <textarea
          ref={textareaRef}
          value={body}
          disabled={!canEdit || blocked}
          onChange={(e) => handleChange(e.target.value)}
          onKeyDown={handleKey}
          placeholder={placeholder}
          rows={3}
          className="w-full resize-none rounded-md border border-input bg-transparent p-2 text-sm text-foreground shadow-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:cursor-not-allowed disabled:bg-muted disabled:text-muted-foreground"
        />
        <div className="mt-1 flex items-center justify-end gap-2">
          {blocked && (
            <span className="mr-auto text-xs text-muted-foreground">
              {disabledReason}
            </span>
          )}
          {isStreaming ? (
            <Button
              type="button"
              variant="destructive"
              size="sm"
              onClick={handleStopClick}
            >
              stop
            </Button>
          ) : null}
          {!canEdit && holderIsPresent && !holderIsIdle ? (
            <Button type="button" variant="outline" size="sm" onClick={onTakeOver}>
              take over
            </Button>
          ) : null}
          <Button type="button" size="sm" disabled={!canSend} onClick={handleSend}>
            send
          </Button>
        </div>
      </div>
    </div>
  );
}
