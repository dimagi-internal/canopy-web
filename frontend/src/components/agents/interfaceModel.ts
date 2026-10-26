// The model the interface form edits. It IS the server's parsed interface (the same JSON `GET
// /interface` returns and `PUT` takes): the form changes a copy of it field by field and sends the
// copy back, so whatever the form has no control for (`pages`, `version`, a field added to the
// parser tomorrow) rides along untouched. ACE's published interface loads and saves unchanged.

export type Capability = {
  description?: string
  callers?: string[]
  entry?: string | null
  tools?: string[]
  bash?: string[]
  read_paths?: string[]
  write_paths?: string[]
  input?: Record<string, string>
  pages?: string[]
  sites?: string[]
  ceiling?: string[]
  // Per site: the agent's own credential names that are its login there (e.g.
  // { 'connect-labs': ['LABS_MCP_TOKEN'] }). Withheld from every turn held on that site's page.
  own_login?: Record<string, string[]>
  data?: string
}

export type InterfaceModel = {
  version?: number
  full?: string[]
  capabilities?: Record<string, Capability>
  callers_default?: string
  // 'confined' sends workspace editors through the capabilities like any member; absent = 'full'.
  editors?: 'full' | 'confined'
}

export const CALLER_BASES = [
  { value: 'member', label: 'Workspace members' },
  { value: 'contact', label: 'Contacts (outside the workspace)' },
  { value: 'unknown', label: 'Unidentified callers' },
] as const

export type CallerParts = { base: string; domain: string; verified: boolean }

const CALLER = /^(member|contact|unknown)(?:@([^:]+))?(?::(verified))?$/

// `contact@dimagi.com:verified` → { base: 'contact', domain: 'dimagi.com', verified: true }.
export function parseCaller(rule: string): CallerParts {
  const m = CALLER.exec(rule)
  return m ? { base: m[1], domain: m[2] ?? '', verified: Boolean(m[3]) } : { base: 'contact', domain: '', verified: false }
}

export function formatCaller({ base, domain, verified }: CallerParts): string {
  return `${base}${domain.trim() ? `@${domain.trim().toLowerCase()}` : ''}${verified ? ':verified' : ''}`
}

// A caller class in words: `contact@dimagi.com:verified` → "Verified contacts at dimagi.com".
export function describeCaller(rule: string): string {
  const m = CALLER.exec(rule)
  if (!m) return rule
  const noun = { member: 'workspace members', contact: 'contacts', unknown: 'unidentified callers' }[m[1]] ?? m[1]
  const words = `${m[3] ? 'verified ' : ''}${noun}${m[2] ? ` at ${m[2]}` : ''}`
  return words.charAt(0).toUpperCase() + words.slice(1)
}

// A rule's effect, as a sentence the owner can check: the preview under each row.
export function previewFull(rule: string): string {
  return `${describeCaller(rule)} get everything its admins get.`
}

export function previewCapability(rule: string, name: string): string {
  return `${describeCaller(rule)} can use ${name || 'this capability'}.`
}

export function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T
}

// Lines in a textarea ⇄ a list of strings.
export const toLines = (list: string[] | undefined): string => (list ?? []).join('\n')
export const fromLines = (text: string): string[] =>
  text.split('\n').map((l) => l.trim()).filter(Boolean)

export function withCapability(model: InterfaceModel, name: string, cap: Capability): InterfaceModel {
  return { ...model, capabilities: { ...(model.capabilities ?? {}), [name]: cap } }
}

export function withoutCapability(model: InterfaceModel, name: string): InterfaceModel {
  const caps = { ...(model.capabilities ?? {}) }
  delete caps[name]
  return { ...model, capabilities: caps }
}

// Renaming keeps the card where it is.
export function renamedCapability(model: InterfaceModel, from: string, to: string): InterfaceModel {
  const caps: Record<string, Capability> = {}
  for (const [n, c] of Object.entries(model.capabilities ?? {})) caps[n === from ? to : n] = c
  return { ...model, capabilities: caps }
}

const READ_ONLY_TOOLS = ['Read', 'Grep', 'Glob', 'mcp__*canopy-web__who_is_asking']

// The two doors most owners want, ready to adjust.
export function askDoor(slug: string): Capability {
  return {
    description: `Ask ${slug} a question.`,
    callers: ['member'],
    entry: `/${slug}:ask --thread {thread_id}`,
    tools: [...READ_ONLY_TOOLS],
    bash: [],
    read_paths: ['{cwd}/**'],
    write_paths: [],
    input: {},
    pages: [],
    sites: [],
    ceiling: [],
  }
}

export function siteDoor(site: string): Capability {
  return {
    description: `Ask about what you are looking at on ${site}.`,
    callers: ['member'],
    entry: null,
    tools: ['Read', 'Grep', 'Glob', 'Skill', 'ToolSearch', 'mcp__*canopy-web__who_is_asking', 'mcp__*canopy-web__current_page'],
    bash: [],
    read_paths: ['{cwd}/**'],
    write_paths: [],
    input: {},
    pages: [],
    sites: [site],
    ceiling: [],
  }
}

export function uniqueName(model: InterfaceModel, wanted: string): string {
  const taken = new Set(Object.keys(model.capabilities ?? {}))
  const base = wanted.toLowerCase().replace(/[^a-z0-9_]/g, '_').replace(/^[^a-z]+/, 'x') || 'door'
  let name = base
  for (let i = 2; taken.has(name); i++) name = `${base}_${i}`
  return name
}

// What the server's parser said, by the field it is about. `ask.tools` belongs to the tools
// field of the `ask` card; `ask` to the card itself; `full` to the whole-agent rules.
export type Problem = { readonly path: string; readonly message: string }

export function problemsFor(problems: readonly Problem[], path: string): string[] {
  return problems.filter((p) => p.path === path).map((p) => p.message)
}
