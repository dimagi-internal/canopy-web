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
  const res = await apiV2.GET('/api/brain/outputs/', { params: { query: { limit: 300 } }, headers: pin(ws) })
  return unwrap(res, 'Failed to load outputs') as BrainOutput[]
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

export type BrainTarget = components['schemas']['BrainTargetOut']
export type BrainTargetIn = components['schemas']['BrainTargetIn']

/** Aim a learner at `ws` (or a workspace below it, or a project there), on a schedule of its own. */
export async function addTarget(ws: string, body: BrainTargetIn): Promise<BrainTarget> {
  const res = await apiV2.POST('/api/brain/targets', { body, headers: pin(ws) })
  return unwrap(res, 'Could not add the target') as BrainTarget
}

/** Take a learner's target away; its schedule goes with it. */
export async function removeTarget(ws: string, id: number): Promise<void> {
  const res = await apiV2.DELETE('/api/brain/targets/{target_id}', { params: { path: { target_id: id } }, headers: pin(ws) })
  unwrap(res, 'Could not remove the target')
}

/** Run one target now: its schedule's own Run now. The server says why when it cannot start. */
export async function runTargetNow(ws: string, id: number): Promise<BrainRun> {
  const res = await apiV2.POST('/api/brain/targets/{target_id}/runs', {
    params: { path: { target_id: id } },
    headers: pin(ws),
  })
  return unwrap(res, 'Could not start the run') as BrainRun
}

export type BrainFlag = components['schemas']['BrainFlagOut']

/** Acknowledge something a learning run made that a removal could not undo (its owner or the workspace's managers). */
export async function acknowledgeFlag(ws: string, id: number): Promise<void> {
  const res = await apiV2.POST('/api/brain/flags/{flag_id}/acknowledge', {
    params: { path: { flag_id: id } },
    headers: pin(ws),
  })
  unwrap(res, 'Could not acknowledge it')
}

export type BrainOutputStatus = 'open' | 'accepted' | 'dismissed' | 'done'

/** Say where a finding stands (its audience's actors only: a system finding's workspace owners and admins). */
export async function setOutputStatus(ws: string, id: number, status: BrainOutputStatus): Promise<BrainOutput> {
  const res = await apiV2.POST('/api/brain/outputs/{output_id}/status', {
    params: { path: { output_id: id } },
    body: { status },
    headers: pin(ws),
  })
  return unwrap(res, 'Could not change its status') as BrainOutput
}

export type BrainTargetBudgetIn = components['schemas']['BrainTargetBudgetIn']

/** Change a target's budget (its workspace's owners and admins). */
export async function setTargetBudget(ws: string, id: number, body: BrainTargetBudgetIn): Promise<BrainTarget> {
  const res = await apiV2.PATCH('/api/brain/targets/{target_id}/budget', {
    params: { path: { target_id: id } },
    body,
    headers: pin(ws),
  })
  return unwrap(res, 'Could not change the budget') as BrainTarget
}

export type BrainWhy = components['schemas']['BrainWhyOut']

/** Why `learner` may or may not read a session you are in or read yourself. */
export async function whyNotRead(ws: string, sessionId: string, learner: string): Promise<BrainWhy> {
  const res = await apiV2.GET('/api/brain/why/{session_id}', {
    params: { path: { session_id: sessionId }, query: { learner } },
    headers: pin(ws),
  })
  return unwrap(res, 'Could not look that up') as BrainWhy
}
