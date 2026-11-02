import { openSlackThread, slackLabel } from "@/lib/slackLinks";
import type { SlackThread } from "./SlackThreadMenuItems";

/** Header chip beside "Connected": this session lives in a Slack thread; click to open it in Slack. */
export function SlackThreadChip({ thread }: { thread: SlackThread }) {
  return (
    <button
      type="button"
      data-testid="slack-thread-chip"
      title="Open this session's Slack thread"
      onClick={() => openSlackThread(thread)}
      className="inline-flex items-center gap-1.5 rounded-full border border-border bg-muted/40 px-2 py-0.5 text-xs text-foreground hover:bg-muted"
    >
      Slack · {slackLabel(thread)}
    </button>
  );
}
