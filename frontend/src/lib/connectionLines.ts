/** Plain words for a Salesforce call a screen declares: "Read Leads", never `GET sobjects/Lead/*`. */
export interface DeclaredLine {
  method: string
  path: string
}

/** A line in words. `code` is a call nothing here recognises, shown as it is (`ConnectionLine`). */
export interface LineWords {
  words: string
  code?: string
}

function plural(type: string): string {
  if (/y$/i.test(type)) return `${type.slice(0, -1)}ies`
  if (/(s|x)$/i.test(type)) return `${type}es`
  return `${type}s`
}

/** `who`: whose records a search reaches, "you" or the agent the screen acts as. */
export function lineWords({ method, path }: DeclaredLine, who = 'you'): LineWords {
  const m = method.toUpperCase()
  if (m === 'GET' && path === 'sobjects/*/*') return { words: 'Read any record' }
  const object = path.match(/^sobjects\/([A-Za-z0-9_]+)(\/\*)?$/)
  if (object) {
    const verb = m === 'GET' ? 'Read' : m === 'DELETE' ? 'Delete' : m === 'POST' && !object[2] ? 'Create' : 'Change'
    return { words: `${verb} ${plural(object[1])}` }
  }
  const flow = path.match(/^actions\/custom\/flow\/([A-Za-z0-9_]+)$/)
  if (flow && m === 'POST') return { words: `Run the flow ${flow[1]}` }
  const action = path.match(/^apexrest\/([A-Za-z0-9_]+)$/)
  if (action && m === 'POST') return { words: `Run the Salesforce action ${action[1]}` }
  if (m === 'GET' && (path === 'query' || path === 'search')) return { words: `Search any record ${who} can see` }
  return { words: 'Other Salesforce access:', code: `${method} ${path}` }
}

export function describeLine(line: DeclaredLine, who = 'you'): string {
  const { words, code } = lineWords(line, who)
  return code ? `${words} ${code}` : words
}

/** Each thing a screen may do, once: what deletes first, then what else changes, then what only reads. */
export function describeLines(
  { reads = [], writes = [] }: { reads?: readonly DeclaredLine[]; writes?: readonly DeclaredLine[] },
  who = 'you',
): LineWords[] {
  const isDelete = (l: DeclaredLine) => l.method.toUpperCase() === 'DELETE'
  const ordered = [...writes.filter(isDelete), ...writes.filter((l) => !isDelete(l)), ...reads]
  const seen = new Map<string, LineWords>()
  for (const line of ordered) {
    const said = describeLine(line, who)
    if (!seen.has(said)) seen.set(said, lineWords(line, who))
  }
  return [...seen.values()]
}
