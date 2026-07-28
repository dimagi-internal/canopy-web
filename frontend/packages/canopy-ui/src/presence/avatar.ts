// Fixed palette. Chosen for legibility against white text in both themes;
// index is picked by a stable hash of the email so a person keeps one color
// everywhere, in every app, across sessions.
const COLORS = [
  'bg-sky-600',
  'bg-emerald-600',
  'bg-violet-600',
  'bg-amber-600',
  'bg-rose-600',
  'bg-teal-600',
  'bg-indigo-600',
  'bg-fuchsia-600',
]

function hash(value: string): number {
  // djb2. Not cryptographic — we only need stable bucketing.
  let h = 5381
  for (let i = 0; i < value.length; i++) h = ((h << 5) + h + value.charCodeAt(i)) | 0
  return Math.abs(h)
}

/**
 * Initials + a stable color class for one person.
 *
 * Color keys on email rather than display name so changing your name does
 * not change your color out from under people who have learned it.
 */
export function avatarFor(email: string, name: string): { initials: string; colorClass: string } {
  const source = (name || email.split('@')[0] || '?').trim()
  const words = source.split(/[\s._-]+/).filter(Boolean)
  const initials =
    words.length >= 2
      ? (words[0][0] + words[1][0]).toUpperCase()
      : (words[0]?.[0] ?? '?').toUpperCase()
  return { initials, colorClass: COLORS[hash(email) % COLORS.length] }
}
