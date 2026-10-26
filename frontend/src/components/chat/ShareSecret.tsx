import { useCallback, useEffect, useState } from "react";
import {
  deleteSecret,
  getSecretRecipients,
  listSecrets,
  shareSecret,
  type SecretRecipients,
  type SharedSecret,
} from "@/api/chat";
import { getZkRecipients } from "@/api/vault";
import { SecretSealCard } from "@/components/vault/SecretSealCard";
import { fingerprint, jkt, sealValue } from "@/vault/zk";

/**
 * "Secrets", opened from the chat's session menu: what this chat holds, and a
 * form to add one.
 *
 * Typing a token into the chat puts it in the transcript, the runner's
 * terminal and the model's context. A secret added here is sealed in THIS
 * browser (`zks1`) to the device keys of the machine running the chat, and
 * canopy keeps only that — it cannot read it. NOTHING is posted into the
 * conversation: mention it by name ("put GH_TOKEN in the repo's Actions
 * secrets"), and the session bound to this chat finds it with `canopy secret
 * list` and spends it with `canopy secret exec`, which opens it with that
 * machine's key and masks it out of the output. Only that session can use it,
 * and it is deleted 30 minutes after it was shared.
 */
export function expiresIn(expiresAt: string, now: number = Date.now()): string {
  const minutes = Math.ceil((new Date(expiresAt).getTime() - now) / 60_000);
  return minutes <= 0 ? "expiring" : `expires in ${minutes} min`;
}

/** `gh token` → `GH_TOKEN`, exactly as the server's `secrets.normalize_name`: the seal
 *  binds the name, so it is shaped here, before sealing. "" when it is not env-var shaped. */
export function secretName(raw: string): string {
  const cleaned = raw.trim().replace(/[^A-Za-z0-9_]+/g, "_").replace(/^_+|_+$/g, "").toUpperCase();
  return /^[A-Z][A-Z0-9_]{0,63}$/.test(cleaned) ? cleaned : "";
}

/** The plaintext cap, as the server's `secrets.VALUE_MAX` was. */
const VALUE_MAX = 16_384;

/** Seal `value` to every key the server named, each target recomputed from its key here. */
export async function sealForChat(r: SecretRecipients, name: string, value: string): Promise<string> {
  if (!r.recipients.length) throw new Error(r.refusal ?? "Nothing can open a secret in this chat yet.");
  const to = await Promise.all(r.recipients.map(async (x) => ({
    kind: "device" as const, target: await jkt(x.public_jwk), publicJwk: x.public_jwk,
  })));
  return sealValue(value, r.binding_id, name, r.kv, to);
}

export function ShareSecretForm({ sessionId, agentSlug, onDone }: {
  sessionId: string;
  /** The chat's agent: a name it declares goes to ITS vault, zero-knowledge. */
  agentSlug?: string | null;
  onDone: () => void;
}) {
  const [shared, setShared] = useState<SharedSecret[] | null>(null);
  const [name, setName] = useState("");
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [justAdded, setJustAdded] = useState("");
  const [declared, setDeclared] = useState<string[]>([]);

  const [sealTo, setSealTo] = useState<SecretRecipients | null>(null);
  const [prints, setPrints] = useState<string[]>([]);

  const refresh = useCallback(() => {
    listSecrets(sessionId).then(setShared).catch(() => setShared([]));
  }, [sessionId]);

  // The machine running this chat, whose keys the value is sealed to. Asked
  // again after a refused share: the chat may have moved to another machine.
  const [askRecipients, setAskRecipients] = useState(0);
  useEffect(() => {
    let live = true;
    getSecretRecipients(sessionId).then(
      async (r) => {
        if (!live) return;
        setSealTo(r);
        const fps = await Promise.all(r.recipients.map(async (x) => fingerprint(await jkt(x.public_jwk))));
        if (live) setPrints(fps);
      },
      (e) => live && setError(e instanceof Error ? e.message : "Could not read who this chat's secrets go to."),
    );
    return () => {
      live = false;
    };
  }, [sessionId, askRecipients]);

  useEffect(refresh, [refresh]);

  // The agent's declared secrets (its admins can read the list): one of those
  // belongs in the agent's vault, where it stays — not in this chat, where it
  // lives 30 minutes.
  useEffect(() => {
    if (!agentSlug) return;
    let live = true;
    getZkRecipients(agentSlug).then((r) => live && setDeclared([...r.declared]), () => undefined);
    return () => {
      live = false;
    };
  }, [agentSlug]);
  const toVault = !!agentSlug && declared.includes(name.trim());

  const submit = async () => {
    if (!name.trim() || !value.trim() || !sealTo) return;
    const shaped = secretName(name);
    if (!shaped) {
      setError("A name starts with a letter and uses only letters, digits and _ (like GH_TOKEN).");
      return;
    }
    if (value.trim().length > VALUE_MAX) {
      setError(`The value is longer than ${VALUE_MAX} characters.`);
      return;
    }
    setBusy(true);
    setError("");
    try {
      // Sealed here; canopy-web receives only what the chat's machine can open.
      const saved = await shareSecret(sessionId, shaped, await sealForChat(sealTo, shaped, value.trim()));
      setValue(""); // drop it from memory as soon as the server has it
      setName("");
      setJustAdded(saved.name);
      refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not share the secret.");
      setAskRecipients((n) => n + 1);
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
          Never posted in the chat. Sealed in your browser to the machine running this chat, so canopy
          cannot read it; only this session can use it, without seeing it, and it is deleted after 30
          minutes. A secret the agent itself keeps needing belongs in its vault instead.
        </p>
        {sealTo?.refusal ? (
          <p className="text-[12px] text-warning" role="status" data-testid="secret-refusal">
            {sealTo.refusal}
          </p>
        ) : sealTo && sealTo.recipients.length > 0 ? (
          <p className="text-[12px] text-muted-foreground" data-testid="secret-seal-to">
            Sealed to <span className="text-foreground">{sealTo.runner_name}</span>
            {prints.length > 0 && (
              <>
                {" "}
                · key <span className="font-mono">{prints.join(", ")}</span>
              </>
            )}
          </p>
        ) : null}
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
        {toVault && agentSlug ? (
          <div data-testid="secret-to-vault">
            <p className="text-[12px] text-muted-foreground">
              <span className="font-mono">{name.trim()}</span> is one of {agentSlug}&apos;s declared secrets, so it
              goes to its vault — zero-knowledge, readable only by its machines and admins.
            </p>
            <SecretSealCard slug={agentSlug} name={name.trim()} />
          </div>
        ) : (
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
        )}
        {error && <p className="text-destructive">{error}</p>}
        <div className="flex justify-end gap-2">
          <button type="button" onClick={onDone} className="rounded-md px-3 py-1.5 text-muted-foreground hover:bg-muted">
            Done
          </button>
          <button
            type="submit"
            disabled={busy || !name.trim() || !value.trim() || !sealTo?.recipients.length}
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
