/** The status / categorical accents of the token contract. `neutral` is the
 *  muted grey for "nothing to say" states; the rest are the semantic tokens. */
export type StatusTone = 'neutral' | 'primary' | 'success' | 'warning' | 'info' | 'special' | 'destructive'

// Written out in full (never built from the tone name) so Tailwind's scanner
// sees every class.
const TINTED: Record<StatusTone, string> = {
  neutral: 'bg-muted text-muted-foreground border-border',
  primary: 'bg-primary/10 text-primary border-primary/30',
  success: 'bg-success/10 text-success border-success/30',
  warning: 'bg-warning/10 text-warning border-warning/30',
  info: 'bg-info/10 text-info border-info/30',
  special: 'bg-special/10 text-special border-special/30',
  destructive: 'bg-destructive/10 text-destructive border-destructive/30',
}

const TEXT: Record<StatusTone, string> = {
  neutral: 'text-muted-foreground',
  primary: 'text-primary',
  success: 'text-success',
  warning: 'text-warning',
  info: 'text-info',
  special: 'text-special',
  destructive: 'text-destructive',
}

const DOT: Record<StatusTone, string> = {
  neutral: 'bg-muted-foreground',
  primary: 'bg-primary',
  success: 'bg-success',
  warning: 'bg-warning',
  info: 'bg-info',
  special: 'bg-special',
  destructive: 'bg-destructive',
}

/** The tinted-badge recipe: `bg-<t>/10 text-<t> border-<t>/30` (neutral is the
 *  muted fill). Pair it with a `border` width class. */
export function toneClasses(tone: StatusTone): string {
  return TINTED[tone]
}

/** Just the text colour of a tone — for a status word inline in a sentence. */
export function toneText(tone: StatusTone): string {
  return TEXT[tone]
}

/** A solid fill of a tone — for a status dot. */
export function toneDot(tone: StatusTone): string {
  return DOT[tone]
}
