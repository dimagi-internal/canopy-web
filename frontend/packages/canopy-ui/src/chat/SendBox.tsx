import {
  useEffect,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import type React from "react";

import type { Draft } from "./protocol";
import { isDraftIdle, msUntilDraftIdle } from "./drafts";
import { Button } from "../ui/button";

/** An attachment the composer is holding, uploaded but not yet sent. */
export interface PendingAttachment {
  id: string;
  filename: string;
  /** Set while the upload is still in flight — the chip renders as busy and
   *  cannot be removed yet, because there is no id on the server to remove. */
  uploading?: boolean;
  /** Upload failed; the chip explains why and is dismissible. */
  error?: string;
}

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
  /** messageId is null when the turn is still QUEUED — no reply exists yet.
   *  The server cancels every non-terminal turn regardless, so a null id is
   *  a valid cancel, not a no-op. */
  onStop: (messageId: string | null) => void;
  onTakeOver: () => void;
  /** Optional app-supplied banner rendered above the composer (e.g. an
   *  imported-session note). The kit itself has no CLI-auth banners. */
  banner?: ReactNode;
  /** When set, sending is disabled and this reason is shown as a hint. */
  disabledReason?: string;
  /** Files staged for the next send. Omit to hide attaching entirely — the kit
   *  stays usable by hosts that have no upload endpoint. */
  attachments?: PendingAttachment[];
  /** Hand off chosen files. The host owns the upload (the kit knows no REST
   *  paths); it re-renders `attachments` as they progress. */
  onAttach?: (files: File[]) => void;
  onRemoveAttachment?: (id: string) => void;
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
  attachments,
  onAttach,
  onRemoveAttachment,
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

  const canAttach = typeof onAttach === "function" && canEdit && !blocked;
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);

  const take = (files: FileList | null | undefined) => {
    if (!canAttach || !files || files.length === 0) return;
    onAttach!(Array.from(files));
  };

  // Paste is the point on desktop: a screenshot goes to the clipboard, and
  // making people save it to disk first is most of the friction.
  const handlePaste = (e: React.ClipboardEvent<HTMLTextAreaElement>) => {
    if (!canAttach) return;
    const files = Array.from(e.clipboardData?.files ?? []);
    if (files.length === 0) return;
    e.preventDefault();  // else the filename lands in the textarea as text
    onAttach!(files);
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
    // Fires even with no streamingMessageId: while a turn sits QUEUED there is
    // no assistant message to name, and that is exactly when you want out.
    onStop(streamingMessageId);
  };

  const placeholder = blocked
    ? disabledReason
    : !canEdit
      ? "Another teammate is editing…"
      : !draft
        ? "Type a message… (connecting…)"
        : "Type a message… (Enter to send, Shift+Enter for newline)";

  const staged = attachments ?? [];

  return (
    <div
      className="border-t border-border bg-background"
      onDragOver={(e) => {
        if (!canAttach) return;
        e.preventDefault();
        setDragging(true);
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={(e) => {
        if (!canAttach) return;
        e.preventDefault();
        setDragging(false);
        take(e.dataTransfer?.files);
      }}
    >
      {banner}
      <div className={`p-2 ${dragging ? "bg-primary/5 ring-1 ring-inset ring-primary/40" : ""}`}>
        {staged.length > 0 && (
          <ul className="mb-1.5 flex flex-wrap gap-1.5" data-testid="attachment-chips">
            {staged.map((a) => (
              <li
                key={a.id}
                className={`flex items-center gap-1.5 rounded-md border px-2 py-1 text-xs ${
                  a.error
                    ? "border-destructive/40 bg-destructive/10 text-destructive"
                    : "border-border bg-muted text-foreground-secondary"
                }`}
              >
                <span className="max-w-[14rem] truncate">{a.filename}</span>
                {a.uploading && <span className="text-muted-foreground">uploading…</span>}
                {a.error && <span title={a.error}>· {a.error}</span>}
                {!a.uploading && onRemoveAttachment && (
                  <button
                    type="button"
                    aria-label={`Remove ${a.filename}`}
                    onClick={() => onRemoveAttachment(a.id)}
                    className="text-muted-foreground hover:text-foreground"
                  >
                    ×
                  </button>
                )}
              </li>
            ))}
          </ul>
        )}
        <textarea
          ref={textareaRef}
          value={body}
          disabled={!canEdit || blocked}
          onChange={(e) => handleChange(e.target.value)}
          onKeyDown={handleKey}
          onPaste={handlePaste}
          placeholder={placeholder}
          rows={3}
          className="w-full resize-none rounded-md border border-input bg-transparent p-2 text-sm text-foreground shadow-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:cursor-not-allowed disabled:bg-muted disabled:text-muted-foreground"
        />
        <div className="mt-1 flex items-center justify-end gap-2">
          {canAttach && (
            <>
              <input
                ref={fileInputRef}
                type="file"
                multiple
                accept="image/*"
                className="hidden"
                data-testid="attachment-input"
                onChange={(e) => {
                  take(e.target.files);
                  e.target.value = "";  // same file twice in a row must re-fire
                }}
              />
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="mr-auto"
                onClick={() => fileInputRef.current?.click()}
              >
                attach
              </Button>
            </>
          )}
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
