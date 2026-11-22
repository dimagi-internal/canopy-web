// A stored agent's definition (apps/agent_definitions; docs/architecture/stored-agents.md):
// its revisions, the one draft, publish / discard / rollback, and creating one.
import { apiV2 } from './client.v2'
import { ApiError } from './problem'
import type { components } from './generated'

type Schemas = components['schemas']
export type DefinitionOut = Schemas['DefinitionOut']
export type DefinitionRevisionSummaryOut = Schemas['DefinitionRevisionSummaryOut']
export type DefinitionRevisionOut = Schemas['DefinitionRevisionOut']
export type DefinitionDiffOut = Schemas['DefinitionDiffOut']
export type StoredAgentOut = Schemas['StoredAgentOut']

function fail(res: { response: Response; error?: unknown }, fallback: string): never {
  throw ApiError.from(res.response.status, res.error, fallback)
}

export async function getDefinition(slug: string): Promise<DefinitionOut> {
  const res = await apiV2.GET('/api/agents/{slug}/definition', { params: { path: { slug } } })
  if (!res.response.ok || !res.data) fail(res, 'Could not read the definition.')
  return res.data as unknown as DefinitionOut
}

export async function listRevisions(slug: string): Promise<DefinitionRevisionSummaryOut[]> {
  const res = await apiV2.GET('/api/agents/{slug}/definition/revisions', { params: { path: { slug } } })
  if (!res.response.ok || !res.data) fail(res, 'Could not list the revisions.')
  return [...(res.data as unknown as readonly DefinitionRevisionSummaryOut[])]
}

export async function getRevision(slug: string, number: number): Promise<DefinitionRevisionOut> {
  const res = await apiV2.GET('/api/agents/{slug}/definition/revisions/{number}', {
    params: { path: { slug, number } },
  })
  if (!res.response.ok || !res.data) fail(res, 'Could not read the revision.')
  return res.data as unknown as DefinitionRevisionOut
}

export async function getRevisionDiff(slug: string, number: number, against?: number): Promise<DefinitionDiffOut> {
  const res = await apiV2.GET('/api/agents/{slug}/definition/revisions/{number}/diff', {
    params: { path: { slug, number }, query: against === undefined ? {} : { against } },
  })
  if (!res.response.ok || !res.data) fail(res, 'Could not read the changes.')
  return res.data as unknown as DefinitionDiffOut
}

/** One file's text: the route answers text/plain, not JSON. */
export async function getRevisionFile(slug: string, number: number, path: string): Promise<string> {
  const res = await apiV2.GET('/api/agents/{slug}/definition/revisions/{number}/files/{path}', {
    params: { path: { slug, number, path } },
    parseAs: 'text',
  })
  if (!res.response.ok || typeof res.data !== 'string') fail(res, `Could not read ${path}.`)
  return res.data as unknown as string
}

export async function publishRevision(slug: string, number: number): Promise<DefinitionRevisionSummaryOut> {
  const res = await apiV2.POST('/api/agents/{slug}/definition/revisions/{number}/publish', {
    params: { path: { slug, number } },
  })
  if (!res.response.ok || !res.data) fail(res, 'Could not publish the draft.')
  return res.data as unknown as DefinitionRevisionSummaryOut
}

export async function discardRevision(slug: string, number: number): Promise<DefinitionRevisionSummaryOut> {
  const res = await apiV2.POST('/api/agents/{slug}/definition/revisions/{number}/discard', {
    params: { path: { slug, number } },
  })
  if (!res.response.ok || !res.data) fail(res, 'Could not discard the draft.')
  return res.data as unknown as DefinitionRevisionSummaryOut
}

export async function rollbackTo(slug: string, revision: number): Promise<DefinitionRevisionSummaryOut> {
  const res = await apiV2.POST('/api/agents/{slug}/definition/rollback', {
    params: { path: { slug } },
    body: { revision, message: '' },
  })
  if (!res.response.ok || !res.data) fail(res, 'Could not roll back.')
  return res.data as unknown as DefinitionRevisionSummaryOut
}

export interface NewStoredAgent {
  workspace: string
  slug: string
  name: string
  mandate: string
  stakeholders: string
}

export async function createStoredAgent(body: NewStoredAgent): Promise<StoredAgentOut> {
  const res = await apiV2.POST('/api/agents/stored', { body: { drop_refused: false, message: '', ...body } })
  if (!res.response.ok || !res.data) fail(res, 'Could not create the agent.')
  return res.data as unknown as StoredAgentOut
}
