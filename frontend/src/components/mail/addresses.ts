// What a person typed into To / Cc / Bcc. Commas inside a quoted name ("Doe, Jane" <j@x.org>) do not split.
const ADDRESS = /^[^\s@<>,;"()[\]\\]+@[^\s@<>,;"()[\]\\]+\.[^\s@<>,;"()[\]\\]+$/

function pieces(raw: string): string[] {
  const out: string[] = []
  let cur = ''
  let quoted = false
  for (const ch of raw) {
    if (ch === '"') quoted = !quoted
    if (!quoted && (ch === ',' || ch === ';' || ch === '\n')) {
      out.push(cur)
      cur = ''
    } else {
      cur += ch
    }
  }
  out.push(cur)
  return out.map((p) => p.trim()).filter(Boolean)
}

function addressIn(piece: string): string | null {
  const a = (piece.match(/<([^<>]*)>\s*$/)?.[1] ?? piece).trim().toLowerCase()
  return ADDRESS.test(a) ? a : null
}

/** "a@x.org, B <b@y.org>; a@X.org" → ['a@x.org', 'b@y.org'] */
export function parseAddresses(raw: string): string[] {
  const found = pieces(raw).map(addressIn).filter((a): a is string => a !== null)
  return [...new Set(found)]
}

/** What the person typed that is not an address: a send must not silently drop these. */
export function invalidAddresses(raw: string): string[] {
  return pieces(raw).filter((p) => addressIn(p) === null)
}
