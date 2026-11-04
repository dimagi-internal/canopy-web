// The model the interface form edits. It IS the server's parsed interface (the same JSON `GET
// /interface` returns and `PUT` takes): the form changes a copy of it field by field and sends the
// copy back, so whatever the form has no control for (`pages`, `version`, a field added to the
// parser tomorrow) rides along untouched. ACE's published interface loads and saves unchanged.

export type Capability = {
  description?: string
  // A capability says what it is, never who may use it: that is a line on the access list.
  entry?: string | null
  tools?: string[]
  bash?: string[]
  read_paths?: string[]
  write_paths?: string[]
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
  capabilities?: Record<string, Capability>
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

// A new door with nothing allowed yet (everything not listed is refused).
export function blankCapability(): Capability {
  return {
    description: '',
    entry: null,
    tools: ['Read', 'mcp__*canopy-web__who_is_asking'],
    bash: [],
    read_paths: ['{cwd}/**'],
    write_paths: [],
    pages: [],
    sites: [],
    ceiling: [],
  }
}

// The names of read mode: `ask` is an agent's own override of it, `read` canopy's default.
export const isReadMode = (name: string): boolean => name === 'ask' || name === 'read'

// An override starts as a copy of the default profile, so it is edited from what is already there.
// An override REPLACES the default whole: nothing is merged.
export function overrideFromDefault(profile: Record<string, unknown>): Capability {
  const list = (k: string): string[] => (Array.isArray(profile[k]) ? (profile[k] as string[]) : [])
  return {
    description: typeof profile.description === 'string' ? profile.description : '',
    entry: typeof profile.entry === 'string' ? profile.entry : null,
    tools: list('tools'),
    bash: list('bash'),
    read_paths: list('read_paths'),
    write_paths: list('write_paths'),
    pages: [],
    sites: [],
    ceiling: [],
  }
}

export function siteCapability(site: string): Capability {
  return {
    description: `Ask about what you are looking at on ${site}.`,
    entry: null,
    tools: ['Read', 'Grep', 'Glob', 'Skill', 'ToolSearch', 'mcp__*canopy-web__who_is_asking', 'mcp__*canopy-web__current_page'],
    bash: [],
    read_paths: ['{cwd}/**'],
    write_paths: [],
    pages: [],
    sites: [site],
    ceiling: [],
  }
}

export function uniqueName(model: InterfaceModel, wanted: string): string {
  const taken = new Set(Object.keys(model.capabilities ?? {}))
  const base = wanted.toLowerCase().replace(/[^a-z0-9_]/g, '_').replace(/^[^a-z]+/, 'x') || 'capability'
  let name = base
  for (let i = 2; taken.has(name); i++) name = `${base}_${i}`
  return name
}

// What the server's parser said, by the field it is about. `ask.tools` belongs to the tools
// field of the `ask` card; `ask` to the card itself.
export type Problem = { readonly path: string; readonly message: string }

export function problemsFor(problems: readonly Problem[], path: string): string[] {
  return problems.filter((p) => p.path === path).map((p) => p.message)
}
