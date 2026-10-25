import type { ReactNode } from "react";
import { AlertTriangle, ChevronRight, OctagonX } from "lucide-react";

import type { Message, MessageAuthor } from "./protocol";
import { isMine } from "./identity";
import { authorColor } from "./personColor";
import { ToolCallPair } from "./ToolCallPair";
import { CompactionDivider } from "./CompactionDivider";
import { AppView } from "./mcpApps/AppView";
import { useAppHost } from "./mcpApps/context";
import { useSecretRequestHost } from "./secretRequest";
import { useAttachmentHost } from "./attachmentHost";
import { MessageActions } from "./messageActions";

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
  /** Who is looking, so a `user` row can tell "me" from "someone else" —
   *  every row persisted before authorship shipped has `author == null` and
   *  must keep rendering exactly as it always has (right-aligned, no label).
   *  Null for a contact viewer, who is named by `currentContactId`. */
  currentUserId?: number | null;
  /** The viewer when it is a contact (a widget visitor); null for a member. */
  currentContactId?: number | null;
}

/** Someone else's line, or null when it's mine (or nobody recorded). A row
 *  with no `author` predates authorship tracking and is never "someone
 *  else's" — it renders exactly as it always did. */
function otherAuthorOf(
  message: Message, currentUserId?: number | null, currentContactId?: number | null,
): MessageAuthor | null {
  if (message.role !== "user" || !message.author) return null;
  return isMine(message.author, currentUserId, currentContactId) ? null : message.author;
}

/** A `user` row Claude Code wrote, not a person: the summary it leaves when it
 *  compacts the conversation. The runner keeps the transcript record's own
 *  flags on the row as `content.record` (`canopy_transcript.record_flags`). */
function isCompactionSummary(message: Message): boolean {
  const record = message.content?.record as Record<string, unknown> | undefined;
  return message.role === "user" && Boolean(record?.compact_summary);
}

/** The CLI's sentences for a login that is gone — mirrors
 *  `canopy_transcript.signed_out.AUTH_REQUIRED_MARKERS`. Only for rows stored
 *  before the runner flagged API errors (`content.api_error`), and only on a
 *  short row that IS one of them, so an agent writing about `/login` is not
 *  mistaken for one. */
const SIGNED_OUT_LINE = /^(not logged in|please run \/login|invalid api key|oauth session expired|auth token expired or invalid|refresh token expired)\b/i;

/** Claude Code's own error notice on the agent's side — a dead login, a cap,
 *  an API failure — which is not the agent answering. Null for a real reply. */
export function apiErrorOf(message: Message): { signedOut: boolean } | null {
  if (message.role !== "assistant") return null;
  if (message.content?.api_error) return { signedOut: Boolean(message.content.signed_out) };
  const text = message.plaintext.trim();
  if (text.length < 120 && SIGNED_OUT_LINE.test(text)) return { signedOut: true };
  return null;
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
  currentUserId,
  currentContactId,
}: Props) {
  const appHost = useAppHost();
  const secretHost = useSecretRequestHost();
  const attachmentHost = useAttachmentHost();
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
  if (message.role === "tool_result" && message.app && appHost) {
    // A View row with no tool_use beside it — the embed widget, which receives
    // the View but never the raw tool rows (`for_widget`).
    return <AppView app={message.app} />;
  }
  if (message.role === "tool_result" && message.secret_request && secretHost) {
    // An agent's request for a secret, with no tool_use beside it.
    return <>{secretHost.render(message.secret_request)}</>;
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
  // Same treatment for a compaction summary: it sits on the human's side of the
  // transcript, but nobody typed it — as a user bubble it read as the person
  // pasting a wall of text.
  if (isCompactionSummary(message)) {
    return (
      <CompactionDivider>
        <SystemSeedRow
          message={message}
          forceOpen={forceToolOpen}
          renderMarkdown={renderMarkdown}
          label="Summary of earlier messages"
          testId="compaction-summary-row"
        />
      </CompactionDivider>
    );
  }

  const apiError = apiErrorOf(message);
  if (apiError) return <ApiErrorRow message={message} signedOut={apiError.signedOut} />;

  const otherAuthor = otherAuthorOf(message, currentUserId, currentContactId);
  // A teammate's line sits on the agent's side of the column, so it needs its
  // own treatment — in the agent's `bg-muted` it read as the agent talking.
  const bubbleClass =
    message.role === "user"
      ? otherAuthor
        ? "mr-auto border border-border bg-card text-foreground"
        : "ml-auto bg-primary text-primary-foreground"
      : "mr-auto bg-muted text-foreground";
  // Hold the "Thinking…" treatment through the gap between
  // chat.stream_start (status flips to "streaming") and the first
  // chat.delta (text becomes non-empty).
  const showThinking =
    (isPending || isStreaming) && message.role === "assistant" && !text;
  return (
    // `overflow-wrap:anywhere` so one unbreakable token — a long path, a URL,
    // inline code — wraps inside the bubble. Without it the token overflowed
    // `max-w-[80%]` and made the whole transcript scroll sideways into empty
    // space on a phone (2026-09-24). Tables and code blocks keep their own
    // horizontal scroll; this only affects running text.
    <div
      data-turn-index={message.turn_index}
      // Focusable (not tabbable) so a tap on a touch screen reveals the row's
      // actions, as hovering does with a mouse.
      tabIndex={-1}
      className={`group/msg relative my-2 min-w-0 max-w-[80%] rounded-2xl px-4 py-2 text-sm leading-relaxed outline-none [overflow-wrap:anywhere] ${bubbleClass}`}
      aria-live={isStreaming || isPending ? "polite" : undefined}
    >
      {/* Beside the bubble, in the free side of the column, so it never covers
          the words; over its top corner on a phone, where there is no side. */}
      {!showThinking && (
        <MessageActions
          message={message}
          className={`absolute -top-5 right-2 sm:top-auto sm:bottom-0 ${
            message.role === "user" && !otherAuthor
              ? "sm:right-full sm:mr-1"
              : "sm:right-auto sm:left-full sm:ml-1"
          }`}
        />
      )}
      {otherAuthor && (
        // In their colour — the same one as their presence chip and their live
        // draft box — so a teammate's lines are attributable at a glance.
        <div
          data-testid="message-author"
          className={`mb-0.5 text-[11px] font-semibold ${authorColor(otherAuthor).text}`}
        >
          {otherAuthor.name}
        </div>
      )}
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
      {/* The files sent with this line, as the host shows them (a private
          conversation's are sealed, and opened by the host's vault code). */}
      {message.role === "user" && attachmentHost?.render(message)}
      {isError && message.role === "assistant" && (
        <ErrorFooter detail={message.error_detail} hasPartial={Boolean(text)} />
      )}
      {/* Your own line, before canopy has confirmed it. Shown at once so a send
          never looks like it vanished; flagged plainly if it did not go. */}
      {message.role === "user" && isPending && (
        <div className="mt-1 text-[11px] opacity-70" role="status">Sending…</div>
      )}
      {message.role === "user" && isError && (
        <div className="mt-1 flex items-start gap-1 text-[11px] font-medium" role="alert">
          <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" aria-hidden="true" />
          <span>{message.error_detail || "Not sent."}</span>
        </div>
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
  label = "System context",
  testId = "system-seed-row",
}: {
  message: Message;
  forceOpen: boolean | undefined;
  renderMarkdown: RenderMarkdown;
  label?: string;
  testId?: string;
}) {
  const text = message.plaintext;
  const lineCount = countLines(text);
  const openProp = forceOpen === undefined ? undefined : forceOpen;
  return (
    <details
      className="group my-2 mr-auto max-w-[80%] rounded-2xl border border-border bg-muted/40 px-3 py-1.5 text-sm"
      data-testid={testId}
      data-turn-index={message.turn_index}
      {...(openProp !== undefined ? { open: openProp } : {})}
    >
      <summary className="flex cursor-pointer items-center gap-1.5 text-muted-foreground hover:text-foreground select-none list-none [&::-webkit-details-marker]:hidden">
        <ChevronRight className="h-3.5 w-3.5 transition-transform group-open:rotate-90" />
        <span className="text-xs font-medium uppercase tracking-wide">
          {label}
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

/** A message that did not run, said as that — not as a bubble of the agent
 *  answering "Not logged in · Please run /login". The CLI's own words stay
 *  beside it, because they are the evidence. */
function ApiErrorRow({ message, signedOut }: { message: Message; signedOut: boolean }) {
  return (
    <div
      data-testid="api-error-row"
      data-turn-index={message.turn_index}
      className="my-1 mr-auto flex max-w-[80%] items-start gap-1.5 text-xs text-warning"
      role="status"
    >
      <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" aria-hidden="true" />
      <span>
        {signedOut
          ? "Didn't run: Claude isn't signed in on the runner."
          : "Error from Claude Code, not a reply:"}
        <span className="ml-1 text-muted-foreground">“{message.plaintext.trim()}”</span>
      </span>
    </div>
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
