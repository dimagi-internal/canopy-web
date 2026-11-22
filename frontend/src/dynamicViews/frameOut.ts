/**
 * What a sandboxed page may make happen OUTSIDE its frame, checked by the page around it: open an
 * https address in a new tab, change the page's own URL parameters, copy text, save a file. The frame
 * has no `allow-popups`, `allow-downloads` or clipboard (and never `allow-same-origin`), so each is a
 * message the canopy page answers (`DynamicViewFrame.tsx`); these are the checks, free of the DOM.
 * Design: docs/architecture/workspace-apps.md § What a page can do outside its frame.
 */

/** An address a page may open in a new tab: https only, no credentials in it. Null otherwise. */
export function externalUrl(raw: unknown): string | null {
  if (typeof raw !== 'string' || raw.length > 8000) return null
  let u: URL
  try {
    u = new URL(raw)
  } catch {
    return null
  }
  if (u.protocol !== 'https:' || u.username || u.password) return null
  return u.toString()
}

/** Page params a page may write into its URL: at most 30, names ≤ 64, values ≤ 2000, never `draft`. */
export const MAX_PARAMS = 30
export function cleanParams(raw: unknown, reserved: readonly string[] = ['draft']): Record<string, string> | string {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return 'setParams takes an object of names and values'
  const out: Record<string, string> = {}
  for (const [k, v] of Object.entries(raw as Record<string, unknown>)) {
    if (v == null) continue
    if (!/^[A-Za-z0-9_.-]{1,64}$/.test(k)) return `"${k.slice(0, 64)}" is not a parameter name`
    if (reserved.includes(k)) return `"${k}" is canopy's own parameter`
    const s = String(v)
    if (s.length > 2000) return `"${k}" is longer than 2000 characters`
    out[k] = s
  }
  if (Object.keys(out).length > MAX_PARAMS) return `at most ${MAX_PARAMS} parameters`
  return out
}

/** The URL of this page with these params, keeping canopy's own (`draft`) and the hash. */
export function withParams(href: string, params: Record<string, string>, keep: readonly string[] = ['draft']): string {
  const u = new URL(href)
  const kept = keep.map((k) => [k, u.searchParams.get(k)] as const).filter(([, v]) => v != null) as [string, string][]
  u.search = new URLSearchParams([...kept, ...Object.entries(params)]).toString()
  return u.toString()
}

export const MAX_CLIPBOARD_CHARS = 1_000_000
export const MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024
/** Types a browser would run or draw as a document: saved as plain bytes, so a blob of canopy's origin never renders. */
const ACTIVE_TYPES = /^(text\/html|application\/xhtml\+xml|image\/svg\+xml|text\/xml|application\/xml|text\/javascript|application\/javascript|application\/ecmascript|text\/ecmascript)\b/i

export interface Download {
  name: string
  type: string
  data: Blob | ArrayBuffer | Uint8Array | string
}

/** A file to save, checked: a plain name, a harmless type, the bytes within the limit. A string is a refusal. */
export function checkDownload(name: unknown, mime: unknown, data: unknown): Download | string {
  const base = typeof name === 'string'
    // eslint-disable-next-line no-control-regex -- control characters are exactly what a file name must lose
    ? (name.split(/[\\/]/).pop() ?? '').replace(/[:*?"<>|\u0000-\u001f\u007f]+/g, '_').replace(/^[.\s]+/, '').trim().slice(0, 200)
    : ''
  if (!base) return 'download(name, mime, data) needs a file name'
  let type = typeof mime === 'string' && /^[a-z0-9.+-]+\/[a-z0-9.+-]+(;\s*charset=[a-z0-9_-]+)?$/i.test(mime.trim())
    ? mime.trim() : 'application/octet-stream'
  if (ACTIVE_TYPES.test(type)) type = 'application/octet-stream'
  let size: number
  if (typeof data === 'string') size = data.length * 3
  else if (data instanceof ArrayBuffer) size = data.byteLength
  else if (data instanceof Uint8Array) size = data.byteLength
  else if (typeof Blob !== 'undefined' && data instanceof Blob) size = data.size
  else return 'download data must be text, an ArrayBuffer, a Uint8Array or a Blob'
  if (size > MAX_DOWNLOAD_BYTES && (typeof data !== 'string' || new Blob([data]).size > MAX_DOWNLOAD_BYTES)) {
    return 'That file is larger than 25 MB.'
  }
  return { name: base, type, data: data as Download['data'] }
}
