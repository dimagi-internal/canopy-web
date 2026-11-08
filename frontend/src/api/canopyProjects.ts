/**
 * Which of your repos canopy works in (GET/PUT /api/me/canopy-projects/).
 *
 * Part of your canopy account, read by every runner you own. A repo outside it is
 * plain Claude Code: canopy routes no repo turn to it, mirrors no session or
 * transcript from it, and keeps no usage from it. An agent's own repo is always
 * canopy's. Unset is nothing. See apps/harness/models.py::ProjectOptIn.
 */
import { apiV2 } from './client.v2'
import { unwrap } from './problem'

/** Normalized from the generated `ProjectOptInOut` (its nested readonly arrays do
 *  not survive an inferred return type — TS2719 under `tsc -b`). */
export type CanopyProjects = {
  everything: boolean
  projects: string[]
  available: string[]
  /** The workspaces you assigned each repo to (and the ones below them). */
  repo_workspaces: Record<string, string[]>
  /** Where a repo you have not assigned belongs: your boxes' home workspaces. */
  default_workspaces: Record<string, string[]>
  /** The workspaces you may assign a repo to. */
  workspaces: { slug: string; display_name: string }[]
}

function normalize(data: {
  everything?: boolean
  projects?: ReadonlyArray<string>
  available?: ReadonlyArray<string>
  repo_workspaces?: Record<string, ReadonlyArray<string>>
  default_workspaces?: Record<string, ReadonlyArray<string>>
  workspaces?: ReadonlyArray<{ slug: string; display_name: string }>
}): CanopyProjects {
  const lists = (m?: Record<string, ReadonlyArray<string>>) =>
    Object.fromEntries(Object.entries(m ?? {}).map(([k, v]) => [k, [...v]]))
  return {
    everything: data.everything ?? false,
    projects: [...(data.projects ?? [])],
    available: [...(data.available ?? [])],
    repo_workspaces: lists(data.repo_workspaces),
    default_workspaces: lists(data.default_workspaces),
    workspaces: (data.workspaces ?? []).map((w) => ({ ...w })),
  }
}

export async function getCanopyProjects(): Promise<CanopyProjects> {
  const res = await apiV2.GET('/api/me/canopy-projects/')
  const data = unwrap(res, 'could not load your canopy projects')
  return normalize(data as unknown as Parameters<typeof normalize>[0])
}

/** A project you still share your laptop sessions with (GET /api/me/session-sharing/). */
export type SessionShare = {
  project_id: number
  handle: string
  name: string
  workspace: string
  repo: string
  shared: boolean
  team_size: number
}

/** The projects whose team you still share your laptop sessions in their repo with. */
export async function getSessionSharing(): Promise<SessionShare[]> {
  const res = await apiV2.GET('/api/me/session-sharing/')
  const data = unwrap(res, 'could not load your session sharing')
  return [...(data as unknown as SessionShare[])]
}

/** Stop sharing your laptop sessions in a project's repo with its team (`true` is refused: retired). */
export async function setSessionSharing(projectId: number, shared: boolean): Promise<SessionShare> {
  const res = await apiV2.PUT('/api/me/session-sharing/{project_id}', {
    params: { path: { project_id: projectId } },
    body: { shared },
  })
  return unwrap(res, 'could not change your session sharing') as unknown as SessionShare
}

export async function setCanopyProjects(
  everything: boolean,
  projects: string[],
  repoWorkspaces: Record<string, string[]>,
): Promise<CanopyProjects> {
  const res = await apiV2.PUT('/api/me/canopy-projects/', {
    body: { everything, projects, repo_workspaces: repoWorkspaces },
  })
  const data = unwrap(res, 'could not save your canopy projects')
  return normalize(data as unknown as Parameters<typeof normalize>[0])
}
