// Who a task can be given to in a workspace, and its labels and projects — the
// choices every assignee, label and project picker offers. Read once per
// workspace and shared by every picker on the page (a table of fifty rows must
// not fetch the member list fifty times).
import { useEffect, useState } from 'react'

import { listAgents, type AgentOut } from '@/api/agents'
import { listLabels, listProjects, type Label, type Project } from '@/api/work'
import { listMembers, type MemberOut } from '@/api/workspaces'

export interface Directory {
  people: MemberOut[]
  agents: AgentOut[]
  labels: Label[]
  projects: Project[]
}

const EMPTY: Directory = { people: [], agents: [], labels: [], projects: [] }
const cache = new Map<string, Promise<Directory>>()

async function load(workspace: string): Promise<Directory> {
  const [people, agents, labels, projects] = await Promise.all([
    listMembers(workspace).catch(() => [] as MemberOut[]),
    listAgents({ limit: 500 })
      .then((p) => p.items)
      .catch(() => [] as AgentOut[]),
    listLabels(workspace).catch(() => [] as Label[]),
    listProjects('open', workspace).catch(() => [] as Project[]),
  ])
  return {
    people,
    agents,
    labels,
    projects,
  }
}

/** Forget a workspace's directory (a label or project was just made). */
export function invalidateDirectory(workspace: string): void {
  cache.delete(workspace)
}

export function useDirectory(workspace: string): Directory {
  const [dir, setDir] = useState<{ ws: string; dir: Directory } | null>(null)
  useEffect(() => {
    if (!workspace) return
    let cancelled = false
    let p = cache.get(workspace)
    if (!p) {
      p = load(workspace)
      cache.set(workspace, p)
    }
    p.then((d) => !cancelled && setDir({ ws: workspace, dir: d })).catch(() => cache.delete(workspace))
    return () => {
      cancelled = true
    }
  }, [workspace])
  return dir?.ws === workspace ? dir.dir : EMPTY
}
