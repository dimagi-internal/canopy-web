/** Shared date formatting. One place for the "Jun 18, 2026" shape the app prints. */

/** "Jun 18, 2026"; '' for a missing value, the raw input when it cannot be formatted. */
export function fmtDate(iso: string | null | undefined): string {
  if (!iso) return ''
  try {
    return new Date(iso).toLocaleDateString(undefined, {
      month: 'short',
      day: 'numeric',
      year: 'numeric',
    })
  } catch {
    return iso
  }
}

/** "Jun 18, 2026, 2:31 PM"; '' for a missing value, the raw input when it cannot be formatted. */
export function fmtDateTime(iso: string | null | undefined): string {
  if (!iso) return ''
  try {
    return new Date(iso).toLocaleString(undefined, {
      month: 'short',
      day: 'numeric',
      year: 'numeric',
      hour: 'numeric',
      minute: '2-digit',
    })
  } catch {
    return iso
  }
}
