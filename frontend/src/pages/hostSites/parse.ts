
/** Split a textarea of URLs into a list. One per line, and commas too, because
 *  people paste both and neither is wrong. */
export function parseOrigins(raw: string): string[] {
  return raw
    .split(/[\n,]/)
    .map((s) => s.trim().replace(/\/$/, ''))
    .filter(Boolean)
}

/** Split a textarea of PEM blocks into a list.
 *
 *  Several keys is the normal state during a rotation, not an edge case: you
 *  publish the new one beside the old, switch the signer, then remove the old.
 *  Splitting on the END marker rather than on blank lines keeps a key whose
 *  base64 body happens to contain one. */
export function parseKeys(raw: string): string[] {
  const END = '-----END PUBLIC KEY-----'
  return raw
    .split(END)
    .map((chunk) => chunk.trim())
    .filter((chunk) => chunk.includes('BEGIN'))
    .map((chunk) => `${chunk}\n${END}`)
}

/** The origin of the page you are on, which is the value the self-widget needs
 *  and the likeliest thing someone wants for a first connection. */
export function currentOrigin(): string {
  return typeof window === 'undefined' ? '' : window.location.origin
}
