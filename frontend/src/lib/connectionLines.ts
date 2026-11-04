/** Plain words for a Salesforce call a screen declares: "Read Leads", never `GET sobjects/Lead/*`. */
export interface DeclaredLine {
  method: string
  path: string
}

function plural(type: string): string {
  if (/y$/i.test(type)) return `${type.slice(0, -1)}ies`
  if (/(s|x)$/i.test(type)) return `${type}es`
  return `${type}s`
}

export function describeLine({ method, path }: DeclaredLine): string {
  const m = method.toUpperCase()
  const object = path.match(/^sobjects\/([A-Za-z0-9_]+)(?:\/\*)?$/)
  if (object) return `${m === 'GET' ? 'Read' : 'Change'} ${plural(object[1])}`
  const flow = path.match(/^actions\/custom\/flow\/([A-Za-z0-9_]+)$/)
  if (flow && m === 'POST') return `Run the flow ${flow[1]}`
  if (m === 'GET' && (path === 'query' || path === 'search')) return 'Search Salesforce records'
  return `${method} ${path}`
}

export function describeLines(lines: readonly DeclaredLine[]): string[] {
  return [...new Set(lines.map(describeLine))]
}
