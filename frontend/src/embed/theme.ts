import { normalizeTheme, type SafeTheme } from '../../packages/canopy-widget/src/theme'

/**
 * Apply a host's theme to the frame — canopy's own document, which no host CSS
 * can reach. This is the other half of `canopy-widget/src/theme.ts`.
 *
 * The theme is validated AGAIN here, with the same function the loader used.
 * The loader runs in the host's page, where nothing stops a host posting a raw
 * message that skipped it; this runs in canopy's document, where it cannot be
 * skipped. Same code, so the two ends cannot disagree about what is valid.
 *
 * It works by overriding canopy's own semantic tokens (`--primary`, `--radius`,
 * …) on `:root`. The whole panel styles off those tokens and never off raw
 * colours (CLAUDE.md: "Design tokens are the single source of truth"), which is
 * what makes a one-variable accent change reach every button and focus ring at
 * once rather than whichever ones someone remembered.
 */

/** Tokens a theme may set, and the only ones this will ever clear. */
const OWNED = ['--primary', '--primary-foreground', '--ring', '--radius'] as const

export function applyFrameTheme(raw: unknown, root: HTMLElement = document.documentElement): SafeTheme {
  const theme = normalizeTheme(raw as never, () => undefined)

  for (const name of OWNED) root.style.removeProperty(name)
  // On BODY, not the root: body applies `font-sans`, and `--font-sans` sits in a
  // `@theme inline` block — Tailwind bakes its literal value into the rule, so
  // overriding the variable would change nothing. An inline style outranks the
  // class.
  const body = root.ownerDocument.body
  body?.style.removeProperty('font-family')

  if (theme.mode) {
    // The server already rendered the shell's class from the URL (no flash);
    // this is for a live re-theme, and for `auto`, which only the browser can
    // resolve.
    root.classList.toggle('dark', resolve(theme.mode))
  }
  if (theme.accent) {
    root.style.setProperty('--primary', theme.accent)
    root.style.setProperty('--ring', theme.accent)
  }
  if (theme.accentForeground) root.style.setProperty('--primary-foreground', theme.accentForeground)
  if (theme.radius) root.style.setProperty('--radius', theme.radius)
  if (theme.font) body?.style.setProperty('font-family', theme.font)
  return theme
}

function resolve(mode: 'light' | 'dark' | 'auto'): boolean {
  if (mode === 'auto') {
    return typeof window.matchMedia === 'function'
      ? window.matchMedia('(prefers-color-scheme: dark)').matches
      : true
  }
  return mode === 'dark'
}
