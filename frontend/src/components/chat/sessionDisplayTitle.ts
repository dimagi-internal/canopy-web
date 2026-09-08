/**
 * What to SHOW for an emdash-discovered session name.
 *
 * The names arrive already built (`c-<subject>-<disc>`, see the runner's
 * `session_naming.py`) and canopy-web only reads them. Two of six rows in the
 * live list read `c-resume-c-resume-…`, because resuming a session derives the
 * new subject from the old session's NAME, and the old name already began with
 * `c-resume-`. Each resume nests another copy.
 *
 * The real fix belongs wherever that resume subject is built — it should strip
 * an existing task-name shape before prefixing, not stack onto it. This is the
 * display-side mitigation so the list is readable in the meantime, and it is
 * deliberately narrow: it collapses a RUN OF THE SAME repeated marker and
 * nothing else, so it cannot mangle a name that merely happens to repeat a word.
 * The untouched name stays available for the row's `title`.
 */

/** Markers a resume-style flow is known to stack. Lower-case, no delimiters. */
const STACKABLE = ['resume']

export function sessionDisplayTitle(raw: string | null | undefined): string {
  const name = (raw ?? '').trim()
  if (!name) return ''

  // Only the canopy shape is in scope — a human-typed emdash task ("bednet")
  // is theirs, and we do not rewrite it.
  const prefix = name.startsWith('c-') ? 'c-' : ''
  if (!prefix) return name

  const segments = name.slice(prefix.length).split('-')
  const out: string[] = []
  for (let i = 0; i < segments.length; i += 1) {
    const seg = segments[i]
    // `c-resume-c-resume-x`: after the leading `c-` is stripped the repetition
    // reads as `resume, c, resume, x`. Drop a marker that is about to be
    // restated, along with the `c` that separates the two copies.
    if (STACKABLE.includes(seg.toLowerCase())) {
      const next = segments[i + 1]
      const after = segments[i + 2]
      if (next === 'c' && after && after.toLowerCase() === seg.toLowerCase()) {
        i += 1 // skip the interleaved 'c'; the loop re-reads the second marker
        continue
      }
      if (next && next.toLowerCase() === seg.toLowerCase()) continue
    }
    out.push(seg)
  }
  return prefix + out.join('-')
}
