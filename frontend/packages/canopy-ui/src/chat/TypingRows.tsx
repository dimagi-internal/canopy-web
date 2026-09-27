import type { PeerDraft } from "./protocol";

/** Everyone else's box, live — the multiplayer half of the composer. One row
 *  per person, newest edit last, right above your own box. */
export function TypingRows({ peers }: { peers: PeerDraft[] }) {
  if (peers.length === 0) return null;
  return (
    <ul className="space-y-1 border-t border-border bg-background px-3 py-2" aria-live="polite">
      {peers.map((p) =>
        p.body === "" ? (
          <li key={p.author.id} data-testid="typing-row" className="flex min-w-0 gap-2 text-xs">
            <span className="shrink-0 font-medium text-foreground">{p.author.name}</span>
            <span className="shrink-0 text-muted-foreground">is typing…</span>
          </li>
        ) : (
          <li key={p.author.id} data-testid="typing-row" className="flex min-w-0 gap-2 text-xs">
            <span className="shrink-0 font-medium text-foreground">{p.author.name}</span>
            <span className="shrink-0 text-muted-foreground">is typing:</span>
            <span className="min-w-0 truncate italic text-foreground-secondary">{p.body}</span>
          </li>
        ),
      )}
    </ul>
  );
}
