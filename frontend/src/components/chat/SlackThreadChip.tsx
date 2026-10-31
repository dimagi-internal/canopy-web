import { DropdownMenu, DropdownMenuContent, DropdownMenuTrigger } from "canopy-ui/ui";
import { SlackThreadMenuItems, type SlackThread } from "./SlackThreadMenuItems";

/** Header chip beside "Connected": this session lives in a Slack thread; click for its link and Disconnect. */
export function SlackThreadChip({
  thread,
  onDetach,
  detaching,
}: {
  thread: SlackThread;
  onDetach?: () => void;
  detaching?: boolean;
}) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        data-testid="slack-thread-chip"
        title="This session is connected to a Slack thread"
        className="inline-flex items-center gap-1.5 rounded-full border border-border bg-muted/40 px-2 py-0.5 text-xs text-foreground hover:bg-muted"
      >
        Slack · {thread.channel}
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start">
        <SlackThreadMenuItems thread={thread} onDetach={onDetach} detaching={detaching} />
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
