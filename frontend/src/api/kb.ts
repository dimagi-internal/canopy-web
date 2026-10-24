// A workspace project's knowledge base: its sources and its version log
// (apps/agents/kb_api.py).

import { apiV2 } from './client.v2'
import type { components } from './generated'
import { unwrap } from './problem'

export type KbSourceOut = components['schemas']['KbSourceOut']
export type KbVersionOut = components['schemas']['KbVersionOut']

/** Where its documents come from (a curator's crawler). 404: not yours to see. */
export async function listKbSources(workspace: string, handle: string): Promise<KbSourceOut[]> {
  const res = await apiV2.GET('/api/workspace-projects/{workspace}/{handle}/kb/sources', {
    params: { path: { workspace, handle } },
  })
  return unwrap(res, 'listKbSources') as KbSourceOut[]
}

/** The version log, newest first. */
export async function listKbVersions(workspace: string, handle: string): Promise<KbVersionOut[]> {
  const res = await apiV2.GET('/api/workspace-projects/{workspace}/{handle}/kb/versions', {
    params: { path: { workspace, handle } },
  })
  return unwrap(res, 'listKbVersions') as KbVersionOut[]
}
