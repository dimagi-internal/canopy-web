import type { ReactNode } from "react";
import { AlertTriangle, ChevronRight, OctagonX } from "lucide-react";

import type { Message } from "./protocol";
import { ToolCallPair } from "./ToolCallPair";

/** How to render assistant/system markdown. Injected by the app so the kit
 *  stays free of `react-markdown`. Defaults to plain text in a <span>. */
export type RenderMarkdown = (text: string) => ReactNode;

const plainText: RenderMarkdown = (text) => (
  <span className="whitespace-pre-wrap">{text}</span>
);

interface Props {
  message: Message;
  /** When set, expand/collapse this row regardless of native toggle. Lets
   *  MessageList's toolbar drive bulk expand/collapse without having to
   *  duplicate the rendering logic per row. */
  forceToolOpen?: boolean;
  renderMarkdown?: RenderMarkdown;
}

/** Count visible lines for the "▸ System context (N lines)" header. */
function countLines(text: string): number {
  if (!text) return 0;
  return text.split("\n").length;
}

// The backend marks cancelled-by-user turns as status=error with
// error_detail prefixed by "cancelled". Treat that visually as a
// neutral "stopped" state, not a scary "error" state.
function classifyError(detail: string | null) {
  const text = (detail ?? "").trim();
  if (text.toLowerCase().startsWith("cancelled")) {
    return {
      kind: "stopped" as const,
      label: text.replace(/^cancelled/i, "Stopped").trim() || "Stopped",
    };
  }
  return {
    kind: "error" as const,
    label: text || "Something went wrong",
  };
}

/**
 * The animated "the agent has the floor" treatment.
 *
 * Exported because it is rendered from two places that must not drift: inside a
 * real assistant row that has started but has no text yet, and as MessageList's
 * TRAILING row while no assistant row exists at all (see `pendingReply` there).
 */
export function ThinkingIndicator({ label = "Thinking…" }: { label?: string }) {
  return (
    <span className="inline-flex items-center gap-1.5 text-muted-foreground">
      <span className="inline-flex gap-0.5" aria-label="thinking">
        <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-current [animation-delay:-0.3s]" />
        <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-current [animation-delay:-0.15s]" />
        <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-current" />
      </span>
      <span className="text-xs italic">{label}</span>
    </span>
  );
}

export function MessageItem({
  message,
  forceToolOpen,
  renderMarkdown = plainText,
}: Props) {
  const text = message.plaintext;
  const isStreaming = message.status === "streaming";
  const isPending = message.status === "pending";
  const isError = message.status === "error";

  // tool_use and tool_result rows that survived the pairing pass in
  // MessageList didn't find a partner — render as standalone with the
  // same component for visual consistency. The common case (paired
  // tool_use+tool_result) is rendered by MessageList itself via
  // ToolCallPair so we never reach here for those.
  if (message.role === "tool_use") {
    return <ToolCallPair use={message} result={null} forceOpen={forceToolOpen} />;
  }
  if (message.role === "tool_result") {
    // Synthesize a fake "use" message so the pair component can render
    // a uniform header. Defensive — should be rare.
    const fakeUse: Message = {
      ...message,
      role: "tool_use",
      content: { name: "tool_result (orphan)" },
    };
    return (
      <ToolCallPair use={fakeUse} result={message} forceOpen={forceToolOpen} />
    );
  }

  // System messages are seed context for the agent: load-bearing for the
  // assistant's first response, but a wall-of-text from the human reader's
  // POV. Render collapsed by default with a chevron header so the send box
  // stays the focal point on session open.
  if (message.role === "system") {
    return (
      <SystemSeedRow
        message={message}
        forceOpen={forceToolOpen}
        renderMarkdown={renderMarkdown}
      />
    );
  }

  const bubbleClass =
    message.role === "user"
      ? "ml-auto bg-primary text-primary-foreground"
      : "mr-auto bg-muted text-foreground";
  // Hold the "Thinking…" treatment through the gap between
  // chat.stream_start (status flips to "streaming") and the first
  // chat.delta (text becomes non-empty).
  const showThinking =
    (isPending || isStreaming) && message.role === "assistant" && !text;
  return (
    <div
      className={`my-2 max-w-[80%] rounded-2xl px-4 py-2 ${bubbleClass}`}
      aria-live={isStreaming || isPending ? "polite" : undefined}
    >
      {showThinking ? (
        <ThinkingIndicator />
      ) : message.role === "assistant" ? (
        renderMarkdown(text)
      ) : (
        <div className="whitespace-pre-wrap">{text}</div>
      )}
      {isStreaming && text && (
        <span className="ml-1 inline-block h-3 w-1 animate-pulse bg-current align-middle" />
      )}
      {isError && message.role === "assistant" && (
        <ErrorFooter detail={message.error_detail} hasPartial={Boolean(text)} />
      )}
    </div>
  );
}

/**
 * Collapsed-by-default rendering for a seed/system message. Uses a native
 * <details> element so it's keyboard-accessible and screen-reader friendly.
 * ``forceToolOpen`` (from MessageList's bulk toolbar) overrides the native
 * state to match the surrounding tool rows.
 */
function SystemSeedRow({
  message,
  forceOpen,
  renderMarkdown,
}: {
  message: Message;
  forceOpen: boolean | undefined;
  renderMarkdown: RenderMarkdown;
}) {
  const text = message.plaintext;
  const lineCount = countLines(text);
  const openProp = forceOpen === undefined ? undefined : forceOpen;
  return (
    <details
      className="group my-2 mr-auto max-w-[80%] rounded-2xl border border-border bg-muted/40 px-3 py-1.5 text-sm"
      data-testid="system-seed-row"
      {...(openProp !== undefined ? { open: openProp } : {})}
    >
      <summary className="flex cursor-pointer items-center gap-1.5 text-muted-foreground hover:text-foreground select-none list-none [&::-webkit-details-marker]:hidden">
        <ChevronRight className="h-3.5 w-3.5 transition-transform group-open:rotate-90" />
        <span className="text-xs font-medium uppercase tracking-wide">
          System context
        </span>
        {lineCount > 0 && (
          <span className="text-xs text-muted-foreground/70">
            · {lineCount} line{lineCount === 1 ? "" : "s"}
          </span>
        )}
      </summary>
      <div className="mt-2 border-t border-border/40 pt-2 text-foreground">
        {renderMarkdown(text)}
      </div>
    </details>
  );
}

function ErrorFooter({
  detail,
  hasPartial,
}: {
  detail: string | null;
  hasPartial: boolean;
}) {
  const { kind, label } = classifyError(detail);
  const isStopped = kind === "stopped";
  // Stopped = neutral muted treatment; error = amber warning. Avoid
  // destructive red — even a real error is recoverable (the user resends
  // the next turn) and red bubbles reflexively read as "your chat is broken".
  const Icon = isStopped ? OctagonX : AlertTriangle;
  const tone = isStopped ? "text-muted-foreground" : "text-warning";
  return (
    <div
      className={`mt-2 flex items-start gap-1.5 border-t border-border/40 pt-1.5 text-xs italic ${tone}`}
      role="status"
    >
      <Icon className="mt-0.5 h-3 w-3 shrink-0" aria-hidden="true" />
      <span>
        {label}
        {hasPartial ? " · partial response shown above" : ""}
      </span>
    </div>
  );
}
