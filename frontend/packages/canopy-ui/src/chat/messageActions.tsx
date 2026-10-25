import { createContext, useContext, useEffect, useRef, useState, type MouseEvent } from "react";
import { Check, Copy, GitBranch, Pencil } from "lucide-react";

import type { Message } from "./protocol";

/**
 * Per-message actions: Copy on every row, and — when the host offers them —
 * "Fork from here" and "Edit" on the person's own last line.
 *
 * Copy is the kit's own (it needs nothing but the row). Fork is the host's:
 * the kit knows no REST paths, so the page says which rows can be forked and
 * runs the fork itself. Edit is the panel's, because it owns the composer and
 * the queue (`ChatPanel` decides which line is editable).
 */
export interface MessageActionsHost {
  /** Whether "Fork from here" is offered on this row. Omit both to offer none. */
  canFork?: (message: Message) => boolean;
  /** Run the fork. `anchor` is where the button sits on screen, so the host
   *  can show progress or a refusal beside it. */
  onFork?: (message: Message, anchor: { x: number; y: number }) => void;
}

interface MessageActionsValue {
  host: MessageActionsHost | null;
  /** The id of the one row whose Edit is offered, or null. */
  editableId: string | null;
  onEdit: ((message: Message) => void) | null;
}

export const MessageActionsContext = createContext<MessageActionsValue>({
  host: null,
  editableId: null,
  onEdit: null,
});

/** What Copy puts on the clipboard for a tool call: the command (or the
 *  input), then its output. */
export function toolCopyText(use: Message, result: Message | null): string {
  const input = (use.content as { input?: unknown } | undefined)?.input;
  const command =
    input && typeof input === "object" && typeof (input as { command?: unknown }).command === "string"
      ? `$ ${(input as { command: string }).command}`
      : input === undefined
        ? ""
        : JSON.stringify(input, null, 2);
  const output = result?.plaintext ?? "";
  return [command, output].filter((part) => part.trim() !== "").join("\n\n");
}

async function writeClipboard(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

const BUTTON =
  "inline-flex h-6 w-6 items-center justify-center rounded text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-40";

/**
 * The small toolbar a row shows on hover (or keyboard focus, or a tap on a
 * touch screen — the row is focusable for that). Positioned by the caller via
 * `className`; the row must be `relative group/msg`.
 */
export function MessageActions({
  message,
  copyText,
  className = "",
}: {
  message: Message;
  /** What Copy writes; defaults to the row's own text (an assistant reply's
   *  markdown source, not its rendering). */
  copyText?: string;
  className?: string;
}) {
  const { host, editableId, onEdit } = useContext(MessageActionsContext);
  const [copied, setCopied] = useState<"ok" | "failed" | null>(null);
  const timer = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(timer.current), []);

  const text = copyText ?? message.plaintext;
  const forkable = Boolean(host?.onFork && host.canFork?.(message));
  const editable = Boolean(onEdit && editableId != null && editableId === message.id);
  if (!text && !forkable && !editable) return null;

  // Inside a <summary> a click would also open or close the row.
  const swallow = (e: MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
  };

  const copy = async (e: MouseEvent) => {
    swallow(e);
    const ok = await writeClipboard(text);
    setCopied(ok ? "ok" : "failed");
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setCopied(null), 1500);
  };

  const fork = (e: MouseEvent<HTMLButtonElement>) => {
    swallow(e);
    const box = e.currentTarget.getBoundingClientRect();
    host?.onFork?.(message, { x: box.left, y: box.bottom + 4 });
  };

  return (
    <div
      data-testid="message-actions"
      role="toolbar"
      aria-label="Message actions"
      className={`z-10 flex items-center gap-0.5 rounded-md border border-border bg-card p-0.5 text-foreground shadow-sm opacity-0 transition-opacity group-hover/msg:opacity-100 group-focus-within/msg:opacity-100 ${editable ? "[@media(hover:none)]:opacity-100" : ""} ${className}`}
    >
      {editable && (
        <button
          type="button"
          data-testid="message-action-edit"
          aria-label="Edit and resend"
          title="Edit and resend: puts this back in the box below. Sending it starts a new turn; the history stays as it is."
          className={BUTTON}
          onClick={(e) => {
            swallow(e);
            onEdit?.(message);
          }}
        >
          <Pencil className="h-3.5 w-3.5" aria-hidden="true" />
        </button>
      )}
      {text && (
        <button
          type="button"
          data-testid="message-action-copy"
          aria-label={copied === "ok" ? "Copied" : "Copy"}
          title={copied === "failed" ? "Could not copy" : "Copy"}
          className={BUTTON}
          onClick={copy}
        >
          {copied === "ok" ? (
            <Check className="h-3.5 w-3.5 text-success" aria-hidden="true" />
          ) : (
            <Copy className="h-3.5 w-3.5" aria-hidden="true" />
          )}
        </button>
      )}
      {forkable && (
        <button
          type="button"
          data-testid="message-action-fork"
          aria-label="Fork from here"
          title="Fork from here: a new session with the conversation up to the end of this turn"
          className={BUTTON}
          onClick={fork}
        >
          <GitBranch className="h-3.5 w-3.5" aria-hidden="true" />
        </button>
      )}
      {copied === "ok" && <span className="sr-only" role="status">Copied</span>}
    </div>
  );
}
