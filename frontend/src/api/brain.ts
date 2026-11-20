import { apiV2 } from './client.v2'
import type { components } from './generated'
import { unwrap } from './problem'

// The brain's reads, each from a SOURCE: `live` (what canopy holds now) or `preview:<id>` (one of your preview
// snapshots: what the brain would do). The routes and shapes are the same either way; the server answers a preview
// only to its owner, as a person, and only with what they still read.

export type BrainSource = string
export const LIVE: BrainSource = 'live'

export type BrainState = components['schemas']['BrainStateOut']
export type BrainLinkPage = components['schemas']['BrainLinkPageOut']
export type BrainStoredLink = components['schemas']['BrainStoredLink']
export type BrainRunPage = components['schemas']['BrainRunPageOut']
export type BrainRun = components['schemas']['BrainRunState']
export type BrainOutput = components['schemas']['BrainOutputOut']
export type WouldRead = components['schemas']['BrainWouldReadOut']
export type WouldReadRow = components['schemas']['PreviewSummaryRow']
export type PreviewTranscript = components['schemas']['PreviewTranscript']
export type BrainPreview = components['schemas']['PreviewSnapshotOut']

/** Canopy learning as it stands (or as a preview says it would), for you. */
export async function getBrainState(source: BrainSource = LIVE): Promise<BrainState> {
  const res = await apiV2.GET('/api/brain/state', { params: { query: { source } } })
  return unwrap(res, 'Failed to load the brain') as BrainState
}

/** Project links of sessions you read, newest first. */
export async function getBrainLinks(source: BrainSource, before = 0): Promise<BrainLinkPage> {
  const res = await apiV2.GET('/api/brain/state/links', { params: { query: { before, limit: 50, source } } })
  return unwrap(res, 'Failed to load links') as BrainLinkPage
}

/** Learning runs about you or a project you read, newest first. A preview has none. */
export async function getBrainRuns(source: BrainSource, before = 0): Promise<BrainRunPage> {
  const res = await apiV2.GET('/api/brain/state/runs', { params: { query: { before, limit: 25, source } } })
  return unwrap(res, 'Failed to load runs') as BrainRunPage
}

/** What learning produced that you may see, or a preview's recommendations. */
export async function listBrainOutputs(source: BrainSource = LIVE): Promise<BrainOutput[]> {
  const res = await apiV2.GET('/api/brain/outputs/', { params: { query: { limit: 50, source } } })
  return unwrap(res, 'Failed to load outputs') as BrainOutput[]
}

/** Per session: whether each class could read it, and the links canopy would make. Live: the last day. */
export async function getWouldRead(source: BrainSource, after = ''): Promise<WouldRead> {
  const res = await apiV2.GET('/api/brain/would-read', { params: { query: { source, after, limit: 10 } } })
  return unwrap(res, 'Failed to load sessions') as WouldRead
}

/** One session of a window, as a learner would read it (secrets scrubbed). */
export async function getPreviewTranscript(since: string, until: string, session: string): Promise<PreviewTranscript> {
  const res = await apiV2.GET('/api/brain/preview', { params: { query: { since, until, session, limit: 1 } } })
  const body = unwrap(res, 'Failed to load the transcript') as components['schemas']['BrainPreviewOut']
  const one = body.sessions[0]
  if (!one) throw new Error('That session is no longer in the window')
  return one.transcript
}

// ---- previews ------------------------------------------------------------------------------------------

export async function listBrainPreviews(): Promise<BrainPreview[]> {
  return unwrap(await apiV2.GET('/api/brain/previews/'), 'Failed to load your previews') as BrainPreview[]
}

/** Run a preview over the last `hours`: computed by the server, kept a week. */
export async function createBrainPreview(hours: number): Promise<BrainPreview> {
  const until = new Date()
  const since = new Date(until.getTime() - hours * 3600_000)
  const res = await apiV2.POST('/api/brain/previews/', {
    body: { since: since.toISOString(), until: until.toISOString(), view: 'full' },
  })
  return unwrap(res, 'Failed to run a preview') as BrainPreview
}

export async function deleteBrainPreview(id: number): Promise<void> {
  const res = await apiV2.DELETE('/api/brain/previews/{preview_id}/', { params: { path: { preview_id: id } } })
  if (res.error) unwrap(res, 'Failed to delete the preview')
}
