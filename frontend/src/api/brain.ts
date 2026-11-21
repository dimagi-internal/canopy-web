import { apiV2, WORKSPACE_HEADER } from './client.v2'
import type { components } from './generated'
import { unwrap } from './problem'

// The brain page's reads, each PINNED to one workspace (`/api/w/<ws>/brain/…`): that workspace and the ones below it,
// as the viewer's role there allows. Every one is the person's own: the server refuses an agent, a turn's token and
// an MCP client, and answers only with sessions and projects the person already reads.

export type BrainState = components['schemas']['BrainStateOut']
export type BrainLinkPage = components['schemas']['BrainLinkPageOut']
export type BrainStoredLink = components['schemas']['BrainStoredLink']
export type BrainRunPage = components['schemas']['BrainRunPageOut']
export type BrainRun = components['schemas']['BrainRunState']
export type BrainOutput = components['schemas']['BrainOutputOut']

const pin = (ws: string) => ({ [WORKSPACE_HEADER]: ws })

/** Canopy learning as it stands at `ws`: counts, learners, runs, removal work. */
export async function getBrainState(ws: string): Promise<BrainState> {
  return unwrap(await apiV2.GET('/api/brain/state', { headers: pin(ws) }), 'Failed to load the brain') as BrainState
}

/** Stored project links of sessions you read, newest first. */
export async function getBrainLinks(ws: string, before = 0): Promise<BrainLinkPage> {
  const res = await apiV2.GET('/api/brain/state/links', { params: { query: { before, limit: 50 } }, headers: pin(ws) })
  return unwrap(res, 'Failed to load stored links') as BrainLinkPage
}

/** Learning runs at `ws` you may see, newest first. */
export async function getBrainRuns(ws: string, before = 0): Promise<BrainRunPage> {
  const res = await apiV2.GET('/api/brain/state/runs', { params: { query: { before, limit: 25 } }, headers: pin(ws) })
  return unwrap(res, 'Failed to load runs') as BrainRunPage
}

/** What learning produced at `ws` that you may see. */
export async function listBrainOutputs(ws: string): Promise<BrainOutput[]> {
  const res = await apiV2.GET('/api/brain/outputs/', { params: { query: { limit: 50 } }, headers: pin(ws) })
  return unwrap(res, 'Failed to load outputs') as BrainOutput[]
}

/** Start one run of a learner for `ws` (its owners and admins, or a canopy admin); a project learner names the project. */
export async function runLearnerNow(ws: string, slug: string, project = ''): Promise<BrainRun> {
  const res = await apiV2.POST('/api/brain/capabilities/{slug}/runs/', {
    params: { path: { slug } },
    body: { project },
    headers: pin(ws),
  })
  return unwrap(res, 'Could not start the run') as BrainRun
}

export type LearningReads = components['schemas']['LearningReadsOut']
export type BrainLearner = components['schemas']['CapabilityOut']
export type LearnerConfig = components['schemas']['LearnerConfigIn']

/** Turn "Learning reads" on or off at `ws`, or `null` to inherit (its owners and admins, or an ancestor's). */
export async function setLearningReads(ws: string, on: boolean | null): Promise<LearningReads> {
  const res = await apiV2.PUT('/api/brain/learning-reads', { body: { on }, headers: pin(ws) })
  return unwrap(res, 'Could not change learning reads') as LearningReads
}

/** Enable or configure a learner whose operator lives at `ws` or below (its owners and admins). */
export async function configureLearner(ws: string, slug: string, body: LearnerConfig): Promise<BrainLearner> {
  const res = await apiV2.PATCH('/api/brain/learners/{slug}/', { params: { path: { slug } }, body, headers: pin(ws) })
  return unwrap(res, 'Could not change the learner') as BrainLearner
}
