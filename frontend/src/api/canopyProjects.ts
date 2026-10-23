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

export async function setCanopyProjects(everything: boolean, projects: string[]): Promise<CanopyProjects> {
  const res = await apiV2.PUT('/api/me/canopy-projects/', { body: { everything, projects } })
  const data = unwrap(res, 'could not save your canopy projects')
  return normalize(data as unknown as Parameters<typeof normalize>[0])
}
