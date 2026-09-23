import { useEffect, useState } from "react";
import {
  addParticipant,
  listParticipants,
  removeParticipant,
  type Participant,
} from "@/api/chat";

/**
 * "People" on the chat page: who has been given this chat, and (for its owner)
 * giving it to a workspace teammate.
 *
 * Being in the workspace does NOT put you in someone else's chat. The chat
 * socket used to add any member who opened one, which is what made a private
 * conversation reachable by anyone holding its link. Sharing is explicit now,
 * and this is where it happens (apps/canopy_sessions/access.py).
 */
export function ChatPeople({ sessionId, myRole }: { sessionId: string; myRole: string | null }) {
  const [open, setOpen] = useState(false);
  const [people, setPeople] = useState<Participant[] | null>(null);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<"editor" | "viewer">("editor");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const isOwner = myRole === "owner";

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    listParticipants(sessionId)
      .then((rows) => !cancelled && setPeople(rows))
      .catch((e) => !cancelled && setError(e instanceof Error ? e.message : "Could not load people."));
    return () => {
      cancelled = true;
    };
  }, [open, sessionId]);

  const act = async (fn: () => Promise<Participant[]>) => {
    setBusy(true);
    setError("");
    try {
      setPeople(await fn());
      setEmail("");
    } catch (e) {
      setError(e instanceof Error ? e.message : "That did not work.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="relative">
      <button
        type="button"
        data-testid="chat-people"
        onClick={() => setOpen((v) => !v)}
        title="Who has this chat"
        className="rounded-md border border-border bg-card px-2 py-1 text-[12px] text-foreground-secondary hover:bg-muted"
      >
        People
      </button>
      {open && (
        <div className="absolute right-0 top-full z-20 mt-1 w-80 space-y-2 rounded-md border border-border bg-card p-3 text-[12px] shadow-lg">
          {people === null && !error && <p className="text-muted-foreground">Loading…</p>}
          {people && people.length === 0 && (
            <p className="text-muted-foreground">Nobody has been given this chat.</p>
          )}
          {people && people.length > 0 && (
            <table className="w-full">
              <tbody>
                {people.map((p) => (
                  <tr key={p.user_id} className="border-b border-border last:border-0">
                    <td className="py-1 text-foreground">{p.display_name}</td>
                    <td className="py-1 text-muted-foreground">{p.role}</td>
                    <td className="py-1 text-right">
                      {p.role !== "owner" && isOwner && (
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() => void act(() => removeParticipant(sessionId, p.user_id))}
                          className="text-foreground-secondary hover:text-destructive disabled:opacity-50"
                        >
                          Remove
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {isOwner && (
            <form
              className="space-y-2"
              onSubmit={(e) => {
                e.preventDefault();
                if (email.trim()) void act(() => addParticipant(sessionId, email.trim(), role));
              }}
            >
              <div className="flex gap-2">
                <input
                  autoFocus
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  placeholder="teammate@dimagi.com"
                  className="min-w-0 flex-1 rounded-md border border-input bg-input px-2 py-1 text-foreground"
                />
                <select
                  value={role}
                  onChange={(e) => setRole(e.target.value as "editor" | "viewer")}
                  className="rounded-md border border-input bg-input px-1 py-1 text-foreground"
                >
                  <option value="editor">Editor</option>
                  <option value="viewer">Viewer</option>
                </select>
              </div>
              <p className="text-muted-foreground">They must already be in this workspace.</p>
              <div className="flex justify-end">
                <button
                  type="submit"
                  disabled={!email.trim() || busy}
                  className="rounded-md bg-primary px-2 py-1 text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
                >
                  {busy ? "Adding…" : "Add"}
                </button>
              </div>
            </form>
          )}
          {!isOwner && myRole && (
            <p className="text-muted-foreground">Only the chat's owner can add people.</p>
          )}
          {error && <p className="text-destructive">{error}</p>}
        </div>
      )}
    </div>
  );
}
