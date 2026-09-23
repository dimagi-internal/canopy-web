import { useState } from "react";
import { shareToSlackCommand, type ShareMode } from "@/pages/chatPageLogic";

/**
 * "Share to Slack", opened from the chat's session menu: asks the SESSION to summarize itself into
 * a channel, by sending it the canopy plugin's `/canopy:share-to-slack` line.
 * The session writes the summary because it has the context — a second
 * summarizer on the server would only have the transcript canopy happens to
 * hold. Posting and binding are done by the session's MCP call, not by this.
 */
export function ShareToSlackForm({
  onShare,
  onDone,
}: {
  onShare: (command: string) => Promise<void>;
  /** Called after a successful share, or on Cancel. */
  onDone: () => void;
}) {
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
      onDone();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not send the share request.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <form
      className="space-y-3 text-[13px]"
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
          className="w-full rounded-md border border-input bg-input px-2 py-1.5 text-foreground"
        />
      </label>
      <fieldset className="space-y-2">
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
          onClick={onDone}
          className="rounded-md px-3 py-1.5 text-foreground-secondary hover:bg-muted"
        >
          Cancel
        </button>
        <button
          type="submit"
          disabled={!command || busy}
          className="rounded-md bg-primary px-3 py-1.5 text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
        >
          {busy ? "Sending…" : "Share"}
        </button>
      </div>
    </form>
  );
}
