/**
 * How a host dresses the widget — the one theming contract, used by both the
 * launcher (host DOM, in our shadow root) and the panel (canopy's own document,
 * inside the iframe).
 *
 * There are two surfaces and they need two mechanisms, which is why this is a
 * JS option at all rather than "just use CSS":
 *
 *   * The LAUNCHER lives in a shadow root on purpose, so a host's global
 *     `button { … }` cannot deform it. Custom properties are the one thing that
 *     crosses a shadow boundary, so the chrome reads `--canopy-*` and a host may
 *     set them from its own stylesheet with no JS; a host that wants full
 *     control can target `::part(launcher)`. Both are EXPLICIT — a stray global
 *     rule still cannot reach in, which is the isolation that matters.
 *   * The PANEL is a separate document on canopy's origin. No host CSS can ever
 *     reach it, so the only way in is data over the handshake — this object.
 *
 * Every value is validated before it is used, on both sides of the frame: a
 * theme is written into style declarations, and a string like
 * `red; background: url(https://…)` must be dropped rather than interpreted.
 */

export type ThemeMode = 'light' | 'dark' | 'auto'

export interface WidgetTheme {
  /** `dark` is the default and what every host got before this option existed.
   *  `auto` follows the visitor's OS setting. */
  mode?: ThemeMode
  /** The brand colour: the launcher, and inside the panel the Send button,
   *  links and focus rings. Any CSS colour. */
  accent?: string
  /** Text drawn ON the accent. Optional: derived from the accent's luminance
   *  when it is a hex or rgb() colour, white otherwise. */
  accentForeground?: string
  /** Corner radius for the launcher, the panel frame and the controls inside
   *  it. A number is px. */
  radius?: string | number
  /** Font family for the launcher and the panel. The host's font must be
   *  loadable in the frame too — a webfont the host loads is NOT visible inside
   *  canopy's document, so name a system stack or one canopy also serves. */
  font?: string
}

/** A theme whose every present value has been checked. */
export type SafeTheme = {
  mode?: ThemeMode
  accent?: string
  accentForeground?: string
  radius?: string
  font?: string
}

const MODES: readonly ThemeMode[] = ['light', 'dark', 'auto']

/** Characters that let a value escape its declaration or load something. */
const HOSTILE = /[;{}<>\\]|url\s*\(|expression\s*\(|@import|\/\*/i

function supports(property: string, value: string): boolean | null {
  const css = (globalThis as { CSS?: { supports?: (p: string, v: string) => boolean } }).CSS
  return typeof css?.supports === 'function' ? css.supports(property, value) : null
}

const COLOR_SHAPE =
  /^(#[0-9a-f]{3,8}|(rgba?|hsla?|oklch|oklab|lab|lch|color)\([^()]*\)|[a-z]+)$/i
const LENGTH_SHAPE = /^\d+(\.\d+)?(px|rem|em|%)$/
const FONT_SHAPE = /^[\w\s,'"-]+$/

function checked(
  value: string,
  property: string,
  shape: RegExp,
): string | undefined {
  const v = value.trim()
  if (!v || v.length > 200 || HOSTILE.test(v)) return undefined
  // The browser's own parser when it has one; the shape check where it does
  // not (jsdom, and very old engines). Both must agree nothing hostile slipped
  // through — the HOSTILE test above runs regardless.
  const ok = supports(property, v)
  if (ok === false) return undefined
  if (ok === null && !shape.test(v)) return undefined
  return v
}

/**
 * Keep what is valid, drop what is not — and say so, because a host whose
 * accent silently did nothing has no other way to find out why.
 */
export function normalizeTheme(
  theme: WidgetTheme | undefined,
  warn: (message: string) => void = (m) => console.warn(m),
): SafeTheme {
  if (!theme || typeof theme !== 'object') return {}
  const out: SafeTheme = {}

  if (theme.mode !== undefined) {
    if (MODES.includes(theme.mode)) out.mode = theme.mode
    else warn(`canopy: theme.mode must be one of ${MODES.join(', ')} — ignoring ${JSON.stringify(theme.mode)}`)
  }

  const colour = (key: 'accent' | 'accentForeground') => {
    const raw = theme[key]
    if (raw === undefined) return
    const v = typeof raw === 'string' ? checked(raw, 'color', COLOR_SHAPE) : undefined
    if (v) out[key] = v
    else warn(`canopy: theme.${key} is not a CSS colour — ignoring ${JSON.stringify(raw)}`)
  }
  colour('accent')
  colour('accentForeground')

  if (theme.radius !== undefined) {
    const raw = typeof theme.radius === 'number' ? `${theme.radius}px` : theme.radius
    const v =
      typeof raw === 'string' && !(typeof theme.radius === 'number' && theme.radius < 0)
        ? checked(raw, 'border-radius', LENGTH_SHAPE)
        : undefined
    if (v) out.radius = v
    else warn(`canopy: theme.radius is not a length — ignoring ${JSON.stringify(theme.radius)}`)
  }

  if (theme.font !== undefined) {
    const v = typeof theme.font === 'string' ? checked(theme.font, 'font-family', FONT_SHAPE) : undefined
    if (v) out.font = v
    else warn(`canopy: theme.font is not a font-family — ignoring ${JSON.stringify(theme.font)}`)
  }

  if (out.accent && !out.accentForeground) {
    out.accentForeground = readableOn(out.accent)
  }
  return out
}

/**
 * Black or white, whichever reads on `colour` — for the two notations whose
 * channels can be read without a browser (hex, rgb()). Anything else gets
 * white, which the host can override with `accentForeground`.
 */
export function readableOn(colour: string): string {
  const rgb = channels(colour)
  if (!rgb) return '#ffffff'
  const [r, g, b] = rgb.map((c) => {
    const s = c / 255
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4
  })
  const luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b
  // The crossover where black and white have equal contrast (~0.179).
  return luminance > 0.179 ? '#000000' : '#ffffff'
}

function channels(colour: string): [number, number, number] | null {
  const hex = colour.trim().match(/^#([0-9a-f]{3}|[0-9a-f]{6})([0-9a-f]{2})?$/i)
  if (hex) {
    const h = hex[1].length === 3 ? hex[1].split('').map((c) => c + c).join('') : hex[1]
    return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16)) as [number, number, number]
  }
  const rgb = colour.trim().match(/^rgba?\(\s*(\d+)[\s,]+(\d+)[\s,]+(\d+)/i)
  if (rgb) return [Number(rgb[1]), Number(rgb[2]), Number(rgb[3])]
  return null
}

/**
 * The custom properties the launcher's styles read, as a host would set them.
 * Written onto the shadow HOST as inline style, so an explicit `theme` option
 * outranks the same variables inherited from a host stylesheet — the more
 * specific instruction wins.
 */
export function launcherVars(theme: SafeTheme): Record<string, string> {
  const vars: Record<string, string> = {}
  if (theme.accent) vars['--canopy-accent'] = theme.accent
  if (theme.accentForeground) vars['--canopy-accent-foreground'] = theme.accentForeground
  if (theme.radius) vars['--canopy-radius'] = theme.radius
  if (theme.font) vars['--canopy-font'] = theme.font
  return vars
}

/** The mode the chrome itself should draw in (the panel frame, the ×). */
export function resolvedMode(mode: ThemeMode | undefined): 'light' | 'dark' {
  if (mode === 'light') return 'light'
  if (mode === 'auto') {
    const mq = typeof window !== 'undefined' && window.matchMedia
      ? window.matchMedia('(prefers-color-scheme: dark)')
      : null
    return mq?.matches === false ? 'light' : 'dark'
  }
  return 'dark'
}
