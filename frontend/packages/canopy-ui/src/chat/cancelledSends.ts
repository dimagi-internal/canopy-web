import type { CancelledSend, Message } from "./protocol";
import { isUnconfirmed } from "./identity";

/** Id prefix of the synthetic row a cancelled send is drawn as. */
export const CANCELLED_ROW_PREFIX = "cancelled:";

/** Sends the user withdrew, placed in the transcript where they were SENT.
 *
 *  A cancelled send never reaches the transcript, so the server lists it
 *  separately (`CancelledSend`). Drawn at the tail (the old behaviour: an
 *  optimistic row nobody retired), it read as the newest, still-unanswered line
 *  (canopy-web#2092). Here it becomes a user row ordered by `sent_at` among the
 *  messages' `created_at`, carrying `content.cancelled` so the bubble can mute
 *  itself and say who cancelled it.
 *
 *  - An optimistic/unconfirmed row of the same send (same client_id) is dropped:
 *    the cancelled row replaces it.
 *  - A send older than the first loaded message is left out: the list is only the
 *    transcript's tail, and a row at the very top would claim a position it has
 *    no evidence for. */
export function withCancelledSends(messages: Message[], cancelled: CancelledSend[] | undefined): Message[] {
  if (!cancelled || cancelled.length === 0) return messages;
  const cancelledIds = new Set(cancelled.map((c) => c.client_id).filter(Boolean));
  const base = messages.filter(
    (m) =>
      !(
        m.role === "user" &&
        isUnconfirmed(m) &&
        typeof m.content?.client_id === "string" &&
        cancelledIds.has(m.content.client_id)
      ),
  );
  const out = [...base];
  const at = (iso: string) => Date.parse(iso);
  const first = base.length > 0 ? at(base[0].created_at) : NaN;
  for (const c of [...cancelled].sort((a, b) => at(a.sent_at) - at(b.sent_at))) {
    const sent = at(c.sent_at);
    if (Number.isNaN(sent)) continue;
    if (!Number.isNaN(first) && sent < first) continue;
    // After every row sent before it; rows with no usable time stay where they are.
    let idx = out.length;
    for (let i = 0; i < out.length; i++) {
      const t = at(out[i].created_at);
      if (!Number.isNaN(t) && t > sent) { idx = i; break; }
    }
    const prevIndex = idx > 0 ? out[idx - 1].turn_index : 0;
    out.splice(idx, 0, {
      id: `${CANCELLED_ROW_PREFIX}${c.turn_id}`,
      turn_index: prevIndex + 0.5,
      role: "user",
      content: {
        text: c.text,
        ...(c.client_id ? { client_id: c.client_id } : {}),
        cancelled: true,
        cancelled_by: c.cancelled_by,
      },
      plaintext: c.text,
      status: "complete",
      error_detail: null,
      started_at: null,
      completed_at: null,
      created_at: c.sent_at,
      author: c.author,
    });
  }
  return out;
}
