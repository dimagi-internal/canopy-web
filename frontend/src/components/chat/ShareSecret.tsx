import { useEffect, useState } from "react";
import { deleteSecret, listSecrets, shareSecret, type SharedSecret } from "@/api/chat";

/**
 * "Share a secret", opened from the chat's session menu.
 *
 * Typing a token into the chat puts it in the transcript, the runner's
 * terminal and the model's context. This stores it against the session instead
 * and posts only a reference (`canopy-secret://<session>/<NAME>`); the agent
 * spends it with `canopy secret exec`, which injects it into one command and
 * masks it out of the output. The value is write-only here: once saved, this
 * page can say it exists and when it was used, and cannot show it.
 */
export function ShareSecretForm({
  sessionId,
  onPost,
  onDone,
}: {
  sessionId: string;
  /** Sends a chat message — the reference, never the value. */
  onPost: (text: string) => Promise<void>;
  onDone: () => void;
}) {
  const [name, setName] = useState("");
  const [value, setValue] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [shared, setShared] = useState<SharedSecret[]>([]);

  useEffect(() => {
    listSecrets(sessionId).then(setShared).catch(() => setShared([]));
  }, [sessionId]);

  const submit = async () => {
    if (!name.trim() || !value.trim()) return;
    setBusy(true);
    setError("");
    try {
      const saved = await shareSecret(sessionId, name, value, note);
      setValue(""); // drop it from memory as soon as the server has it
      await onPost(saved.message);
      onDone();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not share the secret.");
    } finally {
      setBusy(false);
    }
  };

  const forget = async (n: string) => {
    await deleteSecret(sessionId, n).catch(() => undefined);
    setShared((prev) => prev.filter((s) => s.name !== n));
  };

  return (
    <form
      className="space-y-3 text-[13px]"
      autoComplete="off"
      onSubmit={(e) => {
        e.preventDefault();
        void submit();
      }}
    >
      <p className="text-[12px] text-muted-foreground">
        The value is stored encrypted and never enters the chat. The agent gets a reference and uses
        it without reading it.
      </p>
      <label className="block space-y-1">
        <span className="text-muted-foreground">Name</span>
        <input
          autoFocus
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="GH_TOKEN"
          data-testid="secret-name"
          className="w-full rounded-md border border-input bg-input px-2 py-1.5 font-mono text-foreground"
        />
      </label>
      <label className="block space-y-1">
        <span className="text-muted-foreground">Value</span>
        <input
          type="password"
          autoComplete="new-password"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          data-testid="secret-value"
          className="w-full rounded-md border border-input bg-input px-2 py-1.5 font-mono text-foreground"
        />
      </label>
      <label className="block space-y-1">
        <span className="text-muted-foreground">What to do with it (optional, goes in the chat)</span>
        <input
          value={note}
          onChange={(e) => setNote(e.target.value)}
          placeholder="Add it as an Actions secret on dimagi-internal/canopy"
          className="w-full rounded-md border border-input bg-input px-2 py-1.5 text-foreground"
        />
      </label>
      {error && <p className="text-destructive">{error}</p>}
      <div className="flex justify-end gap-2">
        <button type="button" onClick={onDone} className="rounded-md px-3 py-1.5 text-muted-foreground hover:bg-muted">
          Cancel
        </button>
        <button
          type="submit"
          disabled={busy || !name.trim() || !value.trim()}
          data-testid="secret-share"
          className="rounded-md bg-primary px-3 py-1.5 text-primary-foreground disabled:opacity-50"
        >
          {busy ? "Sharing…" : "Share"}
        </button>
      </div>
      {shared.length > 0 && (
        <div className="space-y-1 border-t border-border pt-2">
          <span className="text-[12px] text-muted-foreground">Already shared with this chat</span>
          {shared.map((s) => (
            <div key={s.name} className="flex items-center justify-between gap-2">
              <span className="font-mono">{s.name}</span>
              <span className="ml-auto text-[12px] text-muted-foreground">
                {s.last_used_at ? "used" : "not used yet"}
              </span>
              <button
                type="button"
                onClick={() => void forget(s.name)}
                className="text-[12px] text-destructive hover:underline"
              >
                Forget
              </button>
            </div>
          ))}
        </div>
      )}
    </form>
  );
}
