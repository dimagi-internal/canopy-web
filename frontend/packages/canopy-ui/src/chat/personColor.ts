/** One colour per person, used everywhere that person appears — their presence
 *  chip, the name on their lines, and the box their live draft is drawn in —
 *  so "the violet one" is the same somebody across the whole chat.
 *
 *  Every chip used to be `bg-muted`, which meant identity rested entirely on
 *  two initials — and initials collide constantly on a small team (two J.K.s
 *  render identically). Hue is derived from the id so a given person is the
 *  same colour in every session, for everyone, with no state to keep.
 *
 *  Colour is never the ONLY cue: every surface that uses one also shows the
 *  person's name (or carries it as the accessible name). Each class is written
 *  out in full because Tailwind finds classes by scanning source text — a
 *  class assembled from parts is never generated. */
export interface PersonColor {
  /** Initials avatar / presence chip: tinted fill + readable text. */
  avatar: string;
  /** The person's name set in their colour (message labels, box headers). */
  text: string;
  /** The accent edge of their live-draft box. */
  edge: string;
  /** A solid ring, for "this person is typing right now". */
  ring: string;
}

const PALETTE: PersonColor[] = [
  {
    avatar: "bg-sky-500/20 text-sky-700 dark:text-sky-200",
    text: "text-sky-700 dark:text-sky-300",
    edge: "border-l-sky-500",
    ring: "ring-sky-500",
  },
  {
    avatar: "bg-emerald-500/20 text-emerald-700 dark:text-emerald-200",
    text: "text-emerald-700 dark:text-emerald-300",
    edge: "border-l-emerald-500",
    ring: "ring-emerald-500",
  },
  {
    avatar: "bg-violet-500/20 text-violet-700 dark:text-violet-200",
    text: "text-violet-700 dark:text-violet-300",
    edge: "border-l-violet-500",
    ring: "ring-violet-500",
  },
  {
    avatar: "bg-amber-500/20 text-amber-700 dark:text-amber-200",
    text: "text-amber-700 dark:text-amber-300",
    edge: "border-l-amber-500",
    ring: "ring-amber-500",
  },
  {
    avatar: "bg-rose-500/20 text-rose-700 dark:text-rose-200",
    text: "text-rose-700 dark:text-rose-300",
    edge: "border-l-rose-500",
    ring: "ring-rose-500",
  },
  {
    avatar: "bg-teal-500/20 text-teal-700 dark:text-teal-200",
    text: "text-teal-700 dark:text-teal-300",
    edge: "border-l-teal-500",
    ring: "ring-teal-500",
  },
];

/** The colour for a canopy user id. Same id, same colour, everywhere. */
export function personColor(userId: number): PersonColor {
  return PALETTE[Math.abs(userId) % PALETTE.length];
}

/** The colour for a message author — a canopy user or a contact. A contact
 *  (widget visitor) has no user id; its contact id is a different number
 *  space, so it is offset to avoid always landing on the colour of the user
 *  with the same id. The name beside it is what actually identifies them. */
export function authorColor(author: { user_id?: number; contact_id?: number }): PersonColor {
  if (author.user_id != null) return personColor(author.user_id);
  return personColor((author.contact_id ?? 0) + 3);
}

/** "Robin Sharma" → "RS". */
export function initials(name: string): string {
  return name
    .split(" ")
    .map((w) => w[0])
    .filter(Boolean)
    .join("")
    .slice(0, 2)
    .toUpperCase();
}
