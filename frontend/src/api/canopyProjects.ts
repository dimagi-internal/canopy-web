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
}

function normalize(data: {
  everything?: boolean
  projects?: ReadonlyArray<string>
  available?: ReadonlyArray<string>
}): CanopyProjects {
  return {
    everything: data.everything ?? false,
    projects: [...(data.projects ?? [])],
    available: [...(data.available ?? [])],
  }
}

export async function getCanopyProjects(): Promise<CanopyProjects> {
  const res = await apiV2.GET('/api/me/canopy-projects/')
  const data = unwrap(res, 'could not load your canopy projects')
  return normalize(data as unknown as Parameters<typeof normalize>[0])
}

/** One project you could share your laptop sessions with (GET /api/me/session-sharing/). */
export type SessionShare = {
  project_id: number
  handle: string
  name: string
  workspace: string
  repo: string
  shared: boolean
  team_size: number
}

/** The projects whose team you could share your laptop sessions in their repo with. */
export async function getSessionSharing(): Promise<SessionShare[]> {
  const res = await apiV2.GET('/api/me/session-sharing/')
  const data = unwrap(res, 'could not load your session sharing')
  return [...(data as unknown as SessionShare[])]
}

/** Share (or stop sharing) your laptop sessions in a project's repo with its team. */
export async function setSessionSharing(projectId: number, shared: boolean): Promise<SessionShare> {
  const res = await apiV2.PUT('/api/me/session-sharing/{project_id}', {
    params: { path: { project_id: projectId } },
    body: { shared },
  })
  return unwrap(res, 'could not change your session sharing') as unknown as SessionShare
}

export async function setCanopyProjects(everything: boolean, projects: string[]): Promise<CanopyProjects> {
  const res = await apiV2.PUT('/api/me/canopy-projects/', { body: { everything, projects } })
  const data = unwrap(res, 'could not save your canopy projects')
  return normalize(data as unknown as Parameters<typeof normalize>[0])
}
