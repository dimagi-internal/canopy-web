/**
 * The words the panel names things by: its agent, the page a conversation was
 * started on, and when.
 *
 * Every label here is built from METADATA — the agent's record, a conversation's
 * `origin_resource` (the page it was started on) and its timestamps — never from
 * what anybody said. A private conversation's words are sealed to its people and
 * canopy cannot read them, so a list that needs to tell twelve private
 * conversations apart has only these to go on.
 */

/** What a person calls the agent: its name, as the rest of canopy shows it. An
 *  agent registered with its slug as its name ("labsdata") reads as a word, not
 *  an identifier — the same fallback canopy's own pages use (`displayAgent`). */
export function agentLabel(agent: { slug: string; name?: string }): string {
  const name = (agent.name ?? '').trim()
  if (name && name !== agent.slug) return name
  return agent.slug ? agent.slug.charAt(0).toUpperCase() + agent.slug.slice(1) : ''
}

/** A page's resource URI (`labs-data-space://12`), as words ("Data space 12").
 *
 *  A host names its pages by URI (`setPageState({ resource })`), and only the host
 *  knows what its URIs mean — so this stays dumb: the scheme, minus a `labs-`
 *  style product prefix, as words; then an id, or the first part of the path.
 *  "" for something that is not a URI. */
export function resourceLabel(uri: string): string {
  const match = /^([a-z][a-z0-9+.-]*):\/\/(.*)$/i.exec((uri ?? '').trim())
  if (!match) return ''
  const words = match[1].split('-')
  // `labs-data-space` → "data space": the product prefix says which site, which
  // the visitor is already on.
  const named = (words.length > 1 ? words.slice(1) : words).join(' ')
  const name = named.charAt(0).toUpperCase() + named.slice(1)
  const path = match[2].split(/[?#]/)[0].replace(/\/+$/, '')
  if (!path) return `${name} page`
  if (/^\d+$/.test(path)) return `${name} ${path}`
  const first = path.split('/')[0]
  return first ? `${name} · ${first.replace(/[-_]/g, ' ')}` : `${name} page`
}

/** What the page around the panel is, from what it last declared: its own
 *  `title` when it gives one, else its resource as words. "" when it names none. */
export function pageLabel(state: Record<string, unknown> | null): string {
  if (!state) return ''
  const title = typeof state.title === 'string' ? state.title.trim() : ''
  if (title) return title.slice(0, 80)
  return typeof state.resource === 'string' ? resourceLabel(state.resource) : ''
}

/** The page a conversation began on, cut the same way canopy keeps it
 *  (`page_state.origin_of`: no query, no fragment), so a row can be matched
 *  against the page the visitor is on now. */
export function resourceKey(uri: unknown): string {
  return typeof uri === 'string' ? uri.split(/[?#]/)[0].trim() : ''
}

/** When something happened, as a person says it: "2:14 pm today",
 *  "yesterday 9:50 pm", "Oct 24, 9:50 pm" (the year only when it is not this one). */
export function whenLabel(iso: string, now: Date = new Date()): string {
  const t = Date.parse(iso)
  if (Number.isNaN(t)) return ''
  const at = new Date(t)
  const time = at.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' }).toLowerCase()
  const day = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime()
  const days = Math.round((day(now) - day(at)) / 86_400_000)
  if (days === 0) return `${time} today`
  if (days === 1) return `yesterday ${time}`
  const date = at.toLocaleDateString(undefined, {
    month: 'short', day: 'numeric', ...(at.getFullYear() !== now.getFullYear() ? { year: 'numeric' } : {}),
  })
  return `${date}, ${time}`
}

export interface ConversationMeta {
  /** When it began (`created_at`). */
  startedAt: string
  /** Where it began (`origin_resource`), "" when not on a page that names one. */
  origin: string
}

/** One row's line of facts: "Started 2:14 pm today · on Data space 12", or
 *  "· on this page" when it began on the page the visitor is on now. */
export function conversationFacts(meta: ConversationMeta, here: string, now: Date = new Date()): string {
  const parts: string[] = []
  const when = whenLabel(meta.startedAt, now)
  if (when) parts.push(`Started ${when}`)
  const origin = resourceKey(meta.origin)
  if (origin && here && origin === resourceKey(here)) parts.push('on this page')
  else if (origin) {
    const label = resourceLabel(origin)
    if (label) parts.push(`on ${label}`)
  }
  return parts.join(' · ')
}
