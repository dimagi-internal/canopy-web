import { apiUrl } from '@/api/base'

// The data half of /get-started (GetStartedPage.tsx): which agents each of the
// reader's workspaces offers, read from the plugin marketplace Claude Code
// installs from (/w/<ws>/marketplace.json, apps/plugins).

/** One installable agent: the plugin name is what `/plugin install` and the
 *  agent's slash commands use. */
export interface InstallableAgent {
  plugin: string
  description: string
}

/** One workspace's catalog and the agents in it. */
export interface WorkspaceCatalog {
  workspace: string
  catalog: string
  agents: InstallableAgent[]
}

interface MarketplaceJson {
  plugins?: Array<{ name?: string; description?: string }>
}

export function catalogName(workspace: string): string {
  return `canopy-${workspace}`
}

/** A workspace's installable agents, read from the same marketplace Claude
 *  Code installs from. null when it cannot be read (not a member, offline). */
export async function loadInstallableAgents(workspace: string): Promise<InstallableAgent[] | null> {
  try {
    const res = await fetch(apiUrl(`/w/${encodeURIComponent(workspace)}/marketplace.json`), {
      credentials: 'same-origin',
      headers: { Accept: 'application/json' },
    })
    if (!res.ok) return null
    const body = (await res.json()) as MarketplaceJson
    return (body.plugins ?? [])
      .filter((p): p is { name: string; description?: string } => typeof p.name === 'string' && p.name !== '')
      .map((p) => ({ plugin: p.name, description: p.description ?? '' }))
  } catch {
    return null
  }
}

/** Every catalog among `workspaces` that offers at least one agent, in order. */
export async function loadCatalogs(workspaces: string[]): Promise<WorkspaceCatalog[]> {
  const results = await Promise.all(workspaces.map((ws) => loadInstallableAgents(ws)))
  return workspaces
    .map((ws, i) => ({ workspace: ws, catalog: catalogName(ws), agents: results[i] ?? [] }))
    .filter((c) => c.agents.length > 0)
}
