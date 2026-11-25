/** Plain words for a Salesforce or Google call a screen declares: "Read Leads", never `GET sobjects/Lead/*`. */
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

/** The Google calls a screen may declare (apps/connections/calls.py `GOOGLE_RULES`). */
function googleWords(m: string, path: string): LineWords {
  if (m === 'GET') {
    if (path === 'gmail/profile') return { words: 'See your Gmail address' }
    if (path === 'gmail/messages' || /^gmail\/(messages|threads)\/[^/]+$/.test(path)) return { words: 'Read your mail' }
    if (path === 'calendar/calendars/primary') return { words: 'See your calendar' }
    if (path === 'calendar/calendars/primary/events') return { words: 'Read your calendar events' }
    if (/^sheets\/[^/]+(\/values:batchGet)?$/.test(path)) return { words: 'Read spreadsheets' }
    if (/^docs\/[^/]+$/.test(path)) return { words: 'Read documents' }
  }
  if (m === 'POST') {
    if (path === 'gmail/drafts') return { words: 'Write Gmail drafts (never send)' }
    if (/^sheets\/[^/]+(\/values:batchUpdate|\/values\/[^/]+:append|:batchUpdate)$/.test(path)) return { words: 'Change spreadsheets' }
    if (/^docs\/[^/]+:batchUpdate$/.test(path)) return { words: 'Change documents' }
    if (path === 'files') return { words: 'Create files in Drive' }
  }
  if (m === 'GET' && path === 'files') return { words: 'List files in Drive' }
  if (m === 'GET' && /^files\/[^/]+$/.test(path)) return { words: 'Read file details in Drive' }
  if (m === 'GET' && /^files\/[^/]+\/listLabels$/.test(path)) return { words: "Read files' labels in Drive" }
  if (m === 'POST' && /^files\/[^/]+\/modifyLabels$/.test(path)) return { words: "Change files' labels in Drive" }
  return { words: 'Other Google access:', code: `${m} ${path}` }
}

/** `who`: whose records a search reaches, "you" or the agent the screen acts as. Salesforce unless `provider` says. */
export function lineWords({ method, path }: DeclaredLine, who = 'you', provider = 'salesforce'): LineWords {
  const m = method.toUpperCase()
  if (provider === 'google') return googleWords(m, path)
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

export function describeLine(line: DeclaredLine, who = 'you', provider = 'salesforce'): string {
  const { words, code } = lineWords(line, who, provider)
  return code ? `${words} ${code}` : words
}

/** Each thing a screen may do, once: what deletes first, then what else changes, then what only reads. */
export function describeLines(
  { reads = [], writes = [], provider = 'salesforce' }: { reads?: readonly DeclaredLine[]; writes?: readonly DeclaredLine[]; provider?: string },
  who = 'you',
): LineWords[] {
  const isDelete = (l: DeclaredLine) => l.method.toUpperCase() === 'DELETE'
  const ordered = [...writes.filter(isDelete), ...writes.filter((l) => !isDelete(l)), ...reads]
  const seen = new Map<string, LineWords>()
  for (const line of ordered) {
    const said = describeLine(line, who, provider)
    if (!seen.has(said)) seen.set(said, lineWords(line, who, provider))
  }
  return [...seen.values()]
}
