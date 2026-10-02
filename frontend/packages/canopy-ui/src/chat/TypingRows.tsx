import type { PeerDraft } from "./protocol";
import { PeerComposers } from "./PeerComposers";

/** @deprecated Use `PeerComposers` inside `SendBox` (its `peers` slot), which
 *  is what `ChatPanel` does. Kept so a consumer that mounts it on its own above
 *  a composer keeps working: it is the same peer boxes, padded the way the old
 *  row strip was. */
export function TypingRows({ peers }: { peers: PeerDraft[] }) {
  if (peers.length === 0) return null;
  return (
    <div className="border-t border-border bg-background px-2 pt-2">
      <PeerComposers peers={peers} />
    </div>
  );
}
