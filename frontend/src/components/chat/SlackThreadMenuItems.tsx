import { DropdownMenuItem } from "canopy-ui/ui";
import { openSlackThread } from "@/lib/slackLinks";

export type SlackThread = {
  channel: string;
  channel_name?: string;
  is_dm?: boolean;
  url?: string;
  app_url?: string;
};

/**
 * The actions on a session's Slack thread: open it in Slack (the app, desktop or
 * phone — never the web client unless canopy has no app link) and disconnect.
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
      {(thread.app_url || thread.url) && (
        <DropdownMenuItem inset data-testid="slack-thread-app-link" onClick={() => openSlackThread(thread)}>
          Open in Slack
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
