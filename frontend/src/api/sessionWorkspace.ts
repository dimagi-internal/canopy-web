/**
 * Where YOUR emdash sessions are filed (GET/PATCH /api/me/session-workspace/).
 *
 * A runner you own reports every open emdash task on your box. One whose project
 * is an agent's follows that agent's workspace; every other new one is filed in
 * the default you set here — or, unset, in the runner's own workspace. The same
 * read also lists the workspaces you can move a session to (editor or better).
 * See apps/canopy_sessions/filing.py.
 */
import { apiV2 } from './client.v2'
import { unwrap } from './problem'

export type SessionWorkspaceChoice = { slug: string; display_name: string }

/** Normalized from the generated `SessionWorkspacePreferenceOut` (whose
 *  defaulted fields are optional there, and whose nested readonly array does
 *  not survive an inferred return type — TS2719 under `tsc -b`). */
export type SessionWorkspacePreference = {
  workspace: string | null
  effective: string | null
  choices: SessionWorkspaceChoice[]
}

function normalize(data: {
  workspace?: string | null
  effective?: string | null
  choices?: ReadonlyArray<{ slug: string; display_name: string }>
}): SessionWorkspacePreference {
  return {
    workspace: data.workspace ?? null,
    effective: data.effective ?? null,
    choices: (data.choices ?? []).map((c) => ({ slug: c.slug, display_name: c.display_name })),
  }
}

export async function getSessionWorkspacePreference(): Promise<SessionWorkspacePreference> {
  const res = await apiV2.GET('/api/me/session-workspace/')
  const data = unwrap(res, 'could not load your session workspace')
  return normalize(data as unknown as Parameters<typeof normalize>[0])
}

/** `null` clears the default (each runner's own workspace is used again). */
export async function setSessionWorkspacePreference(
  workspace: string | null,
): Promise<SessionWorkspacePreference> {
  const res = await apiV2.PATCH('/api/me/session-workspace/', {
    body: { workspace },
  })
  const data = unwrap(res, 'could not save your session workspace')
  return normalize(data as unknown as Parameters<typeof normalize>[0])
}
