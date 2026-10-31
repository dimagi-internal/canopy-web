import { DropdownMenuItem } from "canopy-ui/ui";
import { openInSlackApp } from "@/lib/slackLinks";

export type SlackThread = { channel: string; url?: string; app_url?: string };

/**
 * The actions on a session's Slack thread — open it (app, browser) and
 * disconnect. One list, shown in the chat header's Slack chip and in the ⋯ menu.
 */
export function SlackThreadMenuItems({
  thread,
  onDetach,
  detaching,
}: {
  thread: SlackThread;
  onDetach?: () => void;
  detaching?: boolean;
}) {
  return (
    <>
      {thread.app_url && (
        <DropdownMenuItem inset data-testid="slack-thread-app-link" onClick={() => openInSlackApp(thread.app_url!)}>
          Open Slack thread
        </DropdownMenuItem>
      )}
      {thread.url && (
        <DropdownMenuItem
          inset
          data-testid="slack-thread-link"
          onClick={() => window.open(thread.url, "_blank", "noopener,noreferrer")}
        >
          {thread.app_url ? "Open Slack thread in the browser" : "Open Slack thread"}
        </DropdownMenuItem>
      )}
      {onDetach && (
        <DropdownMenuItem inset data-testid="detach-slack" disabled={detaching} onClick={onDetach}>
          {detaching ? "Disconnecting…" : "Disconnect from Slack"}
        </DropdownMenuItem>
      )}
    </>
  );
}
