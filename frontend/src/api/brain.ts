import { apiV2 } from './client.v2'
import type { components } from './generated'
import { unwrap } from './problem'

// The /brain page's reads. Every one is the person's own: the server refuses an agent, a turn's token and an
// MCP client, and answers only with sessions and projects the person already reads.

export type BrainState = components['schemas']['BrainStateOut']
export type BrainLinkPage = components['schemas']['BrainLinkPageOut']
export type BrainStoredLink = components['schemas']['BrainStoredLink']
export type BrainRunPage = components['schemas']['BrainRunPageOut']
export type BrainRun = components['schemas']['BrainRunState']
export type BrainOutput = components['schemas']['BrainOutputOut']
export type PreviewSummary = components['schemas']['BrainPreviewSummaryOut']
export type PreviewRow = components['schemas']['PreviewSummaryRow']
export type PreviewTranscript = components['schemas']['PreviewTranscript']

/** Canopy learning as it stands, for you: counts, learners, runs, removal work. */
export async function getBrainState(): Promise<BrainState> {
  return unwrap(await apiV2.GET('/api/brain/state'), 'Failed to load the brain') as BrainState
}

/** Stored project links of sessions you read, newest first. */
export async function getBrainLinks(before = 0): Promise<BrainLinkPage> {
  const res = await apiV2.GET('/api/brain/state/links', { params: { query: { before, limit: 50 } } })
  return unwrap(res, 'Failed to load stored links') as BrainLinkPage
}

/** Learning runs about you or a project you read, newest first. */
export async function getBrainRuns(before = 0): Promise<BrainRunPage> {
  const res = await apiV2.GET('/api/brain/state/runs', { params: { query: { before, limit: 25 } } })
  return unwrap(res, 'Failed to load runs') as BrainRunPage
}

/** What learning produced that you may see. */
export async function listBrainOutputs(): Promise<BrainOutput[]> {
  const res = await apiV2.GET('/api/brain/outputs/', { params: { query: { limit: 50 } } })
  return unwrap(res, 'Failed to load outputs') as BrainOutput[]
}

/** One page of what learning WOULD do over your sessions in a window, without transcripts. */
export async function getPreviewSummary(since: string, until: string, after = ''): Promise<PreviewSummary> {
  const res = await apiV2.GET('/api/brain/preview/summary', { params: { query: { since, until, after, limit: 10 } } })
  return unwrap(res, 'Failed to load the preview') as PreviewSummary
}

/** One session of the window, as a learner would read it (secrets scrubbed). */
export async function getPreviewTranscript(since: string, until: string, session: string): Promise<PreviewTranscript> {
  const res = await apiV2.GET('/api/brain/preview', { params: { query: { since, until, session, limit: 1 } } })
  const body = unwrap(res, 'Failed to load the transcript') as components['schemas']['BrainPreviewOut']
  const one = body.sessions[0]
  if (!one) throw new Error('That session is no longer in the window')
  return one.transcript
}
