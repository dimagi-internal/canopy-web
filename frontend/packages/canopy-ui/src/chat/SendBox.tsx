import {
  useEffect,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import type React from "react";

import type { Draft, TypingVisibility } from "./protocol";
import {
  clearStoredDraft,
  defaultDraftStorage,
  readStoredDraft,
  writeStoredDraft,
  type DraftStorage,
} from "./drafts";
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
  isStreaming: boolean;
  streamingMessageId: string | null;
  onUpdate: (body: string) => void;
  onSend: () => void;
  /** messageId is null when the turn is still QUEUED — no reply exists yet.
   *  The server cancels every non-terminal turn regardless, so a null id is
   *  a valid cancel, not a no-op. */
  onStop: (messageId: string | null) => void;
  /** Whether the stop the human asked for actually landed. Rendered next to the
   *  button that asked for it, because that is where the person who pressed it
   *  is looking. Undefined = no stop has been asked for on this turn. */
  stopState?: "requested" | "stopped" | "failed";
  /** Optional app-supplied banner rendered above the composer (e.g. an
   *  imported-session note). The kit itself has no CLI-auth banners. */
  banner?: ReactNode;
  /** Everyone else's live drafts (`PeerComposers`), drawn inside this box
   *  directly above your own textarea so the two read as the same kind of
   *  thing — people writing — at the same width. */
  peers?: ReactNode;
  /** When set, sending is disabled and this reason is shown as a hint. */
  disabledReason?: string;
  /** Files staged for the next send. Omit to hide attaching entirely — the kit
   *  stays usable by hosts that have no upload endpoint. */
  attachments?: PendingAttachment[];
  /** Hand off chosen files. The host owns the upload (the kit knows no REST
   *  paths); it re-renders `attachments` as they progress. */
  onAttach?: (files: File[]) => void;
  onRemoveAttachment?: (id: string) => void;
  /** Persist what is typed under this key (the session id) so it survives
   *  unmounting — routing away and back, or closing the tab. Omit to keep the
   *  purely in-memory behaviour. */
  persistKey?: string;
  /** Storage backing `persistKey`. Defaults to localStorage; inject a fake in
   *  tests, or sessionStorage for per-tab drafts. */
  storage?: DraftStorage | null;
  /** This person's choice of how much of THEIR OWN in-progress message peers
   *  get to see: the words live (`live`), just the fact of typing (`typing`),
   *  or nothing until send (`hidden`). Both this and `onTypingVisibilityChange`
   *  are optional, and the control renders only when BOTH are given — a host
   *  that passes neither (a contact's socket, which has no draft sync) gets no
   *  control at all rather than a dead one. */
  typingVisibility?: TypingVisibility;
  onTypingVisibilityChange?: (visibility: TypingVisibility) => void;
}

export function SendBox({
  draft,
  connected,
  isStreaming,
  streamingMessageId,
  onUpdate,
  onSend,
  onStop,
  stopState,
  banner,
  peers,
  disabledReason,
  attachments,
  onAttach,
  onRemoveAttachment,
  persistKey,
  storage,
  typingVisibility,
  onTypingVisibilityChange,
}: Props) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // LOCAL-FIRST: the textarea's value is local state, never server state.
  // Rendering `draft.body` directly made every inbound frame a chance to
  // overwrite the user mid-keystroke — a stale echo of your own debounced
  // update, or a `session.state` snapshot on reconnect (which replaces state
  // wholesale), would rewind the composer to a body from 150ms ago.
  //
  // Seeded from persisted storage when there is one, because a body typed
  // before an unmount exists NOWHERE else: single-player never mirrors it to
  // the server (see drafts.shouldSyncDraftLive). A stored body wins over
  // `draft.body` — it is strictly newer, being what was in the box when we
  // last left it.
  const store = storage === undefined ? defaultDraftStorage() : storage;
  const [localBody, setLocalBody] = useState(
    () => readStoredDraft(store, persistKey ?? "") ?? draft?.body ?? "",
  );

  // Persistence is a synchronous localStorage write per keystroke — no
  // debounce on purpose. The payload is a chat message, the write is
  // microseconds, and every timer-based alternative has to solve flush-on-
  // unmount and flush-on-tab-close to be correct at exactly the moments this
  // feature exists for.
  const persist = (body: string) => {
    if (persistKey) writeStoredDraft(store, persistKey, body);
  };

  // The panel can swap sessions without remounting (same route, new :id), so
  // the box must follow the key rather than carry one session's text into the
  // next. Adjusted during render rather than in an effect — React's documented
  // shape for "reset state when a prop changes", and the one that avoids a
  // paint showing the previous session's text.
  const [keyOnScreen, setKeyOnScreen] = useState(persistKey);
  if (keyOnScreen !== persistKey) {
    setKeyOnScreen(persistKey);
    setLocalBody(readStoredDraft(store, persistKey ?? "") ?? "");
  }

  // A draft with `id === "local"` is never a co-editor's — it is the LOCAL
  // stand-in a host keeps for a principal with no server-side draft (a
  // contact's `useSessionSocket.sendOverHttp` path, or the widget's start
  // screen `contactDraft`). Its body is always either an echo of what THIS
  // box just typed (harmless to mirror back) or a failed send being restored
  // after `handleSend` already cleared the box — the one case with no other
  // way to reach the screen, since sending clears `localBody` optimistically
  // rather than waiting for the round trip. Scoped to `id === "local"` so a
  // real multiplayer draft (a server-assigned id) is never adopted this way —
  // that was the deleted `theirEdit` effect's mistake, which adopted ANY
  // other editor's draft and could overwrite what you were mid-typing.
  useEffect(() => {
    if (draft?.id === "local" && draft.body !== localBody) {
      setLocalBody(draft.body);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draft?.id, draft?.body]);

  // Typing is ALWAYS allowed now — this box is only ever YOUR OWN draft, and
  // every editor gets one (see protocol.ts::SessionState.active_draft). It
  // used to require `draft != null`, so the composer was disabled until
  // session.state landed — locking you out of your own input on first paint
  // and again on every reconnect. Keystrokes typed early are held locally and
  // flushed when the draft exists (see useSessionSocket.sendChat).
  const body = localBody;
  const blocked = Boolean(disabledReason);
  // Sending needs a draft (`chat.send` commits the SERVER's copy, so there must
  // be one) AND a live socket. The socket check is load-bearing now that the
  // composer clears optimistically: `send()` drops every frame but chat.stop
  // when the socket is closed, so an allowed-but-undeliverable send would clear
  // the box and lose the message outright. Sending stays available while the
  // agent is replying — a send made mid-reply is QUEUED, not blocked, and
  // <QueuedRows> is what shows it landed.
  const canSend =
    connected &&
    draft != null &&
    body.trim().length > 0 &&
    !blocked;

  const handleChange = (value: string) => {
    setLocalBody(value);
    persist(value);
    onUpdate(value);
  };

  const canAttach = typeof onAttach === "function" && !blocked;
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
    // echo back — a round trip the person sending has no reason to wait on.
    setLocalBody("");
    if (persistKey) clearStoredDraft(store, persistKey);
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
    : !draft
      ? "Type a message… (connecting…)"
      : isStreaming
        ? "Type a message — it will be sent after the current reply"
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
        {peers}
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
          data-testid="composer"
          value={body}
          disabled={blocked}
          onChange={(e) => handleChange(e.target.value)}
          onKeyDown={handleKey}
          onPaste={handlePaste}
          placeholder={placeholder}
          rows={3}
          className={[
            "w-full resize-none rounded-md border bg-transparent p-2 text-sm shadow-sm",
            "placeholder:text-muted-foreground focus-visible:outline-none",
            "focus-visible:ring-1 focus-visible:ring-ring disabled:cursor-not-allowed",
            "border-input text-foreground disabled:bg-muted disabled:text-muted-foreground",
          ].join(" ")}
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
          {stopState === "failed" ? (
            // The one state that MUST be loud. Everything else here is either
            // self-evident (the reply stopped) or transient. A stop that did not
            // take looks exactly like a stop that worked — the agent keeps
            // running either way — so without this it is invisible, which is the
            // whole reason Stop could not be trusted.
            <span
              role="status"
              className="mr-auto text-xs font-medium text-destructive"
              title="Escape was pressed and the agent is still running. Try again, or stop it in the terminal."
            >
              stop didn&rsquo;t take — still running
            </span>
          ) : null}
          {/* Never DISABLED while a stop is in flight, only relabelled. If the
              runner dies between the request and the verdict, `stopState` never
              advances — and a button that latched off would leave the human with
              no way to ask again, the exact trap #519 documents for the composer
              lock. Pressing again is harmless: the runner dedupes by session_key,
              so three impatient presses are still one Escape. */}
          {isStreaming ? (
            <Button
              type="button"
              variant="destructive"
              size="sm"
              onClick={handleStopClick}
            >
              {stopState === "requested" ? "stopping…" : "stop"}
            </Button>
          ) : null}
          {typingVisibility != null && onTypingVisibilityChange != null && (
            <label className="flex items-center gap-1 text-xs text-muted-foreground">
              <span className="hidden sm:inline">Others see:</span>
              <select
                data-testid="typing-visibility"
                value={typingVisibility}
                title="Choose what others see of your message while you're still typing it."
                onChange={(e) =>
                  onTypingVisibilityChange(e.target.value as TypingVisibility)
                }
                className={[
                  // bg-background, not bg-transparent: a native <select>'s
                  // OPTION list paints on the OS's own background, and a
                  // transparent trigger left the closed control's text
                  // sitting on whatever was behind it — unreadable in dark
                  // mode, where that was the page's own dark-on-dark text.
                  "rounded-md border border-input bg-background px-1.5 py-1 text-xs",
                  "text-foreground focus-visible:outline-none focus-visible:ring-1",
                  "focus-visible:ring-ring",
                ].join(" ")}
              >
                <option value="live">My text</option>
                <option value="typing">Typing…</option>
                <option value="hidden">Nothing</option>
              </select>
            </label>
          )}
          <Button
            type="button"
            size="sm"
            data-testid="send"
            disabled={!canSend}
            onClick={handleSend}
          >
            send
          </Button>
        </div>
      </div>
    </div>
  );
}
