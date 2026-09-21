import { useState } from "react";
import { shareToSlackCommand, type ShareMode } from "@/pages/chatPageLogic";

/**
 * "Share to Slack" on the chat page: asks the SESSION to summarize itself into
 * a channel, by sending it the canopy plugin's `/canopy:share-to-slack` line.
 * The session writes the summary because it has the context — a second
 * summarizer on the server would only have the transcript canopy happens to
 * hold. Posting and binding are done by the session's MCP call, not by this.
 */
export function ShareToSlack({
  disabledReason,
  onShare,
}: {
  /** Why the session cannot take a message right now; the share is a message. */
  disabledReason?: string;
  onShare: (command: string) => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const [channel, setChannel] = useState("");
  const [mode, setMode] = useState<ShareMode>("broadcast");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const command = shareToSlackCommand(channel, mode);

  const submit = async () => {
    if (!command) return;
    setBusy(true);
    setError("");
    try {
      await onShare(command);
      setOpen(false);
      setChannel("");
      setMode("broadcast");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not send the share request.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="relative">
      <button
        type="button"
        data-testid="share-to-slack"
        onClick={() => setOpen((v) => !v)}
        disabled={Boolean(disabledReason)}
        title={disabledReason || "Have this session summarize what it's doing and post it to Slack"}
        className="rounded-md border border-border bg-card px-2 py-1 text-[12px] text-foreground-secondary hover:bg-muted disabled:opacity-50"
      >
        Share to Slack
      </button>
      {open && (
        <form
          className="absolute right-0 top-full z-20 mt-1 w-72 space-y-2 rounded-md border border-border bg-card p-3 text-[12px] shadow-lg"
          onSubmit={(e) => {
            e.preventDefault();
            void submit();
          }}
        >
          <label className="block space-y-1">
            <span className="text-muted-foreground">Channel</span>
            <input
              autoFocus
              value={channel}
              onChange={(e) => setChannel(e.target.value)}
              placeholder="#connect-dev"
              className="w-full rounded-md border border-input bg-input px-2 py-1 text-foreground"
            />
          </label>
          <fieldset className="space-y-1">
            <label className="flex items-start gap-2">
              <input
                type="radio"
                name="share-mode"
                checked={mode === "broadcast"}
                onChange={() => setMode("broadcast")}
              />
              <span>
                <span className="text-foreground">Broadcast</span>
                <span className="block text-muted-foreground">One post. Replies stay in Slack.</span>
              </span>
            </label>
            <label className="flex items-start gap-2">
              <input type="radio" name="share-mode" checked={mode === "bind"} onChange={() => setMode("bind")} />
              <span>
                <span className="text-foreground">Keep the thread connected</span>
                <span className="block text-muted-foreground">
                  Replies in the thread reach this session, and its replies post there.
                </span>
              </span>
            </label>
          </fieldset>
          {channel && !command && (
            <p className="text-destructive">That doesn't look like a Slack channel name.</p>
          )}
          {error && <p className="text-destructive">{error}</p>}
          <div className="flex justify-end gap-2">
            <button
              type="button"
              onClick={() => setOpen(false)}
              className="rounded-md px-2 py-1 text-foreground-secondary hover:bg-muted"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={!command || busy}
              className="rounded-md bg-primary px-2 py-1 text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
            >
              {busy ? "Sending…" : "Share"}
            </button>
          </div>
        </form>
      )}
    </div>
  );
}
