import type { Message, MessageAuthor } from "./protocol";

/** Is this line the viewer's own? A viewer is a canopy user (`currentUserId`)
 *  OR a contact — a widget visitor, who has no user id and is named by the
 *  snapshot's `current_contact_id` instead. Comparing user ids alone made every
 *  line a contact wrote render as someone else's, under their own name. */
export function isMine(
  author: MessageAuthor | null | undefined,
  currentUserId: number | null | undefined,
  currentContactId: number | null | undefined,
): boolean {
  if (!author) return false;
  return (
    (author.user_id != null && author.user_id === currentUserId) ||
    (author.contact_id != null && author.contact_id === currentContactId)
  );
}

/** The same person, or no way to tell (either side unauthored). */
export function sameAuthor(
  a: MessageAuthor | null | undefined,
  b: MessageAuthor | null | undefined,
): boolean {
  if (!a || !b) return true;
  if (a.user_id != null || b.user_id != null) return a.user_id === b.user_id;
  return a.contact_id === b.contact_id;
}

/** A user row still waiting for the durable line it will become: an optimistic
 *  send (`local:`), a receipted transcript-sourced send (`transient:` — the
 *  server wrote no row, the transcript will), or one still pending/failed. */
export function isUnconfirmed(m: Message): boolean {
  return (
    m.status === "pending" || m.status === "error" ||
    m.id.startsWith("local:") || m.id.startsWith("transient:")
  );
}
