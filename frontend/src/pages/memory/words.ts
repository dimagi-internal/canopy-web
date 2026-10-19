/** Your memory, in a person's words. Pure helpers behind /people/me: they turn
 *  HCP grants, audit events and entries into sentences someone who has never
 *  heard of HCP can read. Nothing here calls the API. */
import type { HcpAuditEvent, HcpGrant, PersonFactDetail } from '../../api/people'
import { MY_CATEGORIES } from '../../api/people'

const CATEGORY_LABEL: Record<string, string> = Object.fromEntries(MY_CATEGORIES.map((c) => [c.value, c.label]))

export function categoryLabel(category: string | null | undefined): string {
  if (!category) return 'Other things'
  return CATEGORY_LABEL[category] ?? category.replace(/_/g, ' ')
}

/** "3 min ago", "5 h ago", "yesterday", "Oct 7". Future times read "in 5 h". */
export function relTime(iso: string, now: Date = new Date()): string {
  const t = new Date(iso).getTime()
  const diff = now.getTime() - t
  const ahead = diff < 0
  const s = Math.abs(diff) / 1000
  const say = (n: number, unit: string) => (ahead ? `in ${n} ${unit}` : `${n} ${unit} ago`)
  if (s < 60) return ahead ? 'in a moment' : 'just now'
  if (s < 3600) return say(Math.round(s / 60), 'min')
  if (s < 86400) return say(Math.round(s / 3600), 'h')
  if (!ahead && s < 2 * 86400) return 'yesterday'
  if (s < 7 * 86400) return say(Math.round(s / 86400), 'days')
  return new Date(iso).toLocaleDateString([], { month: 'short', day: 'numeric' })
}

/** The agent half of a grant's name: "ACE over Slack:t02…" → "ACE". */
export function agentName(g: HcpGrant): string {
  return g.client.name.replace(/\s+(over|on)\s+.*$/, '')
}

/** Where an agent reaches you, without channel ids: "Slack", "the connect-labs app". */
export function placeOf(g: HcpGrant): string {
  if (g.canopy.client) return `${g.canopy.client.name}, an app run by ${g.canopy.client.operator}`
  if (g.canopy.host) return g.canopy.host
  const [kind, where] = (g.canopy.channel || '').split(':')
  switch (kind) {
    case 'slack':
      return 'Slack'
    case 'app_view':
      return where ? `the ${where} app` : 'an app'
    case 'widget':
      return where ? `the chat widget on ${where}` : 'a chat widget'
    case 'email':
      return 'email'
    case '':
    case 'connect':
    case 'chat':
    case 'canopy':
      return 'canopy'
    default:
      return kind.replace(/_/g, ' ')
  }
}

/** How long a grant lasts, as a promise: "Until you take it back" / "This conversation · ends in 5 h". */
export function durationOf(g: HcpGrant, now: Date = new Date()): string {
  if (g.grantType === 'persistent') return 'Until you take it back'
  if (g.expiresAt) return `For one conversation · ends ${relTime(g.expiresAt, now)}`
  return 'For one conversation'
}

/** Why a past grant stopped: "You took it back" / "Ended Oct 14" (the conversation ran out). */
export function endedOf(g: HcpGrant): string {
  if (g.status === 'revoked') return 'You took it back'
  const when = g.expiresAt ? new Date(g.expiresAt).toLocaleDateString([], { month: 'short', day: 'numeric' }) : ''
  return when ? `Ended ${when} with its conversation` : 'Ended with its conversation'
}

/** One row per agent you currently allow: several active grants to one agent
 *  (e.g. kept, plus a conversation one) collapse into one card. */
export interface AgentAccess {
  key: string
  name: string
  grants: HcpGrant[]
  places: string[]
  kept: boolean
}

export function activeAccess(grants: HcpGrant[]): AgentAccess[] {
  const by = new Map<string, AgentAccess>()
  for (const g of grants) {
    if (g.status !== 'active') continue
    const key = g.canopy.agent ?? g.client.id
    const row = by.get(key) ?? { key, name: agentName(g), grants: [], places: [], kept: false }
    row.grants.push(g)
    const place = placeOf(g)
    if (!row.places.includes(place)) row.places.push(place)
    row.kept = row.kept || g.grantType === 'persistent'
    by.set(key, row)
  }
  return Array.from(by.values())
}

/** "agent:ace/model:x" → "ace"; "contact:12" / an email → null (a person). */
export function actorSlug(actorId: string): string | null {
  const m = /^agent:([^/]+)/.exec(actorId)
  return m ? m[1] : null
}

export function actorLabel(e: HcpAuditEvent, names: Map<string, string>): string {
  if (e.actorType === 'user') return 'You'
  if (e.actorType === 'system') return 'Canopy'
  const slug = actorSlug(e.actorId)
  if (!slug) return e.actorId
  return names.get(slug) ?? slug.charAt(0).toUpperCase() + slug.slice(1)
}

/** The agent a grant event is about. canopy writes the grant's client name into
 *  `detail` — bare ("ACE over Slack:t02"), "<name>: <why>", or "client=<name>; …". */
export function grantSubject(detail: string | null): string {
  if (!detail) return 'An agent'
  const name = detail.startsWith('client=') ? detail.slice(7).split(';')[0] : detail.split(': ')[0]
  return name.replace(/\s+(over|on)\s+.*$/, '').trim() || 'An agent'
}

/** Why a search returned an entry (or that nothing matched), and the question it
 *  was asked — from a `preference.read` event's detail, which canopy writes as
 *  "retrievalId=…; returned=N; why=<reason>; query=<question>" (hcp.search). Older
 *  reads carry neither. */
export function readWhy(detail: string | null): { why: string; asked: string } | null {
  if (!detail) return null
  const q = detail.indexOf('; query=')
  const head = q < 0 ? detail : detail.slice(0, q)
  const m = /(?:^|; )why=([^;]*)/.exec(head)
  if (!m) return null
  return { why: m[1].trim(), asked: q < 0 ? '' : detail.slice(q + 8).trim() }
}

/** One audit event as a sentence: `who` + `did`, and an optional `why`. A grant
 *  event is told about the agent it concerns, not whoever performed it. */
export function eventSentence(e: HcpAuditEvent, names: Map<string, string>): { who: string; did: string; why: string } {
  const actor = actorLabel(e, names)
  const about = e.category ? ` — “${categoryLabel(e.category)}”` : ''
  const byYou = e.actorType === 'user'
  const [who, did] = ((): [string, string] => {
    switch (e.eventType) {
      case 'preference.read':
        return [actor, `read your memory${about}`]
      case 'preference.created':
        return [actor, `${byYou ? 'added' : 'remembered'} something new${about}`]
      case 'preference.updated':
        return [actor, `${byYou ? 'corrected' : 'changed'} something${about}`]
      case 'preference.deleted':
        return [actor, `${byYou ? 'forgot' : 'removed'} something${about}`]
      case 'preference.exported':
        return [actor, 'downloaded a copy of your memory']
      case 'grant.issued':
        return [grantSubject(e.detail), byYou ? 'was allowed to read your memory — by you' : 'was allowed to read your memory']
      case 'grant.revoked':
        return [grantSubject(e.detail), byYou ? 'lost access — you took it back' : 'lost access']
      case 'grant.expired':
        return [grantSubject(e.detail), 'lost access — its conversation ended']
      case 'grant.narrowed':
        return [grantSubject(e.detail), 'now reaches less of your memory']
      default:
        return [actor, e.eventType.replace(/[._]/g, ' ')]
    }
  })()
  return { who, did, why: e.purpose ?? '' }
}

/** For each entry: how many times an agent read it, by whom, and when last. */
export interface EntryReads {
  count: number
  last: string
  by: string[]
}

export function readsByEntry(audit: HcpAuditEvent[], names: Map<string, string>): Map<string, EntryReads> {
  const out = new Map<string, EntryReads>()
  for (const e of audit) {
    if (e.eventType !== 'preference.read' || !e.entryId || e.actorType === 'user') continue
    const row = out.get(e.entryId) ?? { count: 0, last: e.timestamp, by: [] }
    row.count += 1
    if (e.timestamp > row.last) row.last = e.timestamp
    const who = actorLabel(e, names)
    if (!row.by.includes(who)) row.by.push(who)
    out.set(e.entryId, row)
  }
  return out
}

/** Where your entries live, which is what decides who reads them: each
 *  organisation with its workspaces, then entries in no workspace (written by you
 *  before a workspace was required, or by an outside app). */
export interface Place {
  /** A workspace slug, or null for entries in no workspace. */
  workspace: string | null
  name: string
  facts: PersonFactDetail[]
}

export interface OrgPlaces {
  org: string
  name: string
  places: Place[]
}

export function groupByPlace(facts: PersonFactDetail[]): { orgs: OrgPlaces[]; unplaced: Place } {
  const orgs = new Map<string, OrgPlaces>()
  const unplaced: Place = { workspace: null, name: 'Not in any workspace', facts: [] }
  for (const f of facts) {
    if (!f.workspace) {
      unplaced.facts.push(f)
      continue
    }
    const key = f.org ?? f.workspace
    const org = orgs.get(key) ?? { org: key, name: f.org_name || key, places: [] }
    let place = org.places.find((p) => p.workspace === f.workspace)
    if (!place) {
      place = { workspace: f.workspace, name: f.workspace_name || f.workspace, facts: [] }
      org.places.push(place)
    }
    place.facts.push(f)
    orgs.set(key, org)
  }
  return { orgs: Array.from(orgs.values()), unplaced }
}

/** A workspace's entries split by the board project they were learned on: each
 *  project in order of its newest entry, then the entries tied to none (null).
 *  An agent working on a project is told that project's entries first. */
export interface ProjectGroup {
  project: { id: number; title: string } | null
  facts: PersonFactDetail[]
}

export function byProject(facts: PersonFactDetail[]): ProjectGroup[] {
  const groups = new Map<number, ProjectGroup>()
  const none: ProjectGroup = { project: null, facts: [] }
  for (const f of facts) {
    if (!f.project) {
      none.facts.push(f)
      continue
    }
    const g = groups.get(f.project.id) ?? { project: { id: f.project.id, title: f.project.title }, facts: [] }
    g.facts.push(f)
    groups.set(f.project.id, g)
  }
  const out = Array.from(groups.values())
  return none.facts.length ? [...out, none] : out
}

/** Does an agent you allow read entries in `workspace` (null = no workspace)? Canopy
 *  says which workspaces a grant reaches; nothing is derived here. */
export function reads(a: AgentAccess, workspace: string | null): boolean {
  return a.grants.some((g) => (g.canopy.reaches ?? []).includes(workspace))
}

/** Day headings for the activity list: "Today", "Yesterday", "Mon, Oct 12". */
export function dayLabel(iso: string, now: Date = new Date()): string {
  const d = new Date(iso)
  const start = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime()
  const days = Math.round((start(now) - start(d)) / 86400000)
  if (days === 0) return 'Today'
  if (days === 1) return 'Yesterday'
  return d.toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' })
}
