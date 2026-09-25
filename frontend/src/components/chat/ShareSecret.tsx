import { useCallback, useEffect, useState } from "react";
import { deleteSecret, listSecrets, shareSecret, type SharedSecret } from "@/api/chat";

/**
 * "Secrets", opened from the chat's session menu: what this chat holds, and a
 * form to add one.
 *
 * Typing a token into the chat puts it in the transcript, the runner's
 * terminal and the model's context. A secret added here is stored encrypted
 * against this chat instead, and NOTHING is posted into the conversation —
 * mention it by name ("put GH_TOKEN in the repo's Actions secrets"), and the
 * session bound to this chat finds it with `canopy secret list` and spends it
 * with `canopy secret exec`, which masks it out of the output. Only that
 * session can use it, and it is deleted 30 minutes after it was shared. The
 * value is write-only here: this page can say a secret exists, never show it.
 */
export function expiresIn(expiresAt: string, now: number = Date.now()): string {
  const minutes = Math.ceil((new Date(expiresAt).getTime() - now) / 60_000);
  return minutes <= 0 ? "expiring" : `expires in ${minutes} min`;
}

export function ShareSecretForm({ sessionId, onDone }: { sessionId: string; onDone: () => void }) {
  const [shared, setShared] = useState<SharedSecret[] | null>(null);
  const [name, setName] = useState("");
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [justAdded, setJustAdded] = useState("");

  const refresh = useCallback(() => {
    listSecrets(sessionId).then(setShared).catch(() => setShared([]));
  }, [sessionId]);

  useEffect(refresh, [refresh]);

  const submit = async () => {
    if (!name.trim() || !value.trim()) return;
    setBusy(true);
    setError("");
    try {
      const saved = await shareSecret(sessionId, name, value);
      setValue(""); // drop it from memory as soon as the server has it
      setName("");
      setJustAdded(saved.name);
      refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not share the secret.");
    } finally {
      setBusy(false);
    }
  };

  const forget = async (n: string) => {
    await deleteSecret(sessionId, n).catch(() => undefined);
    refresh();
  };

  return (
    <div className="space-y-4 text-[13px]">
      <section className="space-y-1" data-testid="secrets-available">
        <span className="text-[12px] text-muted-foreground">Available to this session</span>
        {shared === null ? (
          <p className="text-muted-foreground">Loading…</p>
        ) : shared.length === 0 ? (
          <p className="text-muted-foreground">None yet.</p>
        ) : (
          shared.map((s) => (
            <div key={s.name} className="flex items-center gap-2">
              <span className="font-mono text-foreground">{s.name}</span>
              <span className="ml-auto text-[12px] text-muted-foreground">
                {s.last_used_at ? "used" : "not used yet"} · {expiresIn(s.expires_at)}
              </span>
              <button
                type="button"
                onClick={() => void forget(s.name)}
                className="text-[12px] text-destructive hover:underline"
              >
                Forget
              </button>
            </div>
          ))
        )}
        {justAdded && (
          <p className="text-[12px] text-muted-foreground" role="status">
            Added. Refer to <span className="font-mono">{justAdded}</span> by name in the chat.
          </p>
        )}
      </section>

      <form
        className="space-y-3 border-t border-border pt-3"
        autoComplete="off"
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <p className="text-[12px] text-muted-foreground">
          Stored encrypted and never posted in the chat. Only this session can use it, without seeing
          it, and it is deleted after 30 minutes.
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
        {error && <p className="text-destructive">{error}</p>}
        <div className="flex justify-end gap-2">
          <button type="button" onClick={onDone} className="rounded-md px-3 py-1.5 text-muted-foreground hover:bg-muted">
            Done
          </button>
          <button
            type="submit"
            disabled={busy || !name.trim() || !value.trim()}
            data-testid="secret-share"
            className="rounded-md bg-primary px-3 py-1.5 text-primary-foreground disabled:opacity-50"
          >
            {busy ? "Adding…" : "Add"}
          </button>
        </div>
      </form>
    </div>
  );
}
